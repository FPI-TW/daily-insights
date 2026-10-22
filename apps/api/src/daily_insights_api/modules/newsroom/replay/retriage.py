"""Targeted re-triage: keep a triage replay's events, re-judge only chosen articles.

Re-running triage over a whole range costs thousands of calls, while a prompt
change aimed at one kind of story (energy and shipping) moves only that kind's
judgements. A ``retriage`` run therefore rebuilds a base variant's triage replay
from the cache (event assignment exactly as that run made it), picks the
articles the change can affect, and asks the target variant's triage prompt
about each of them with the very payload (article and candidate events) the
base call saw. Their impact, escalation, story type, new facts and event date
are replaced; topic, directness and the event an article belongs to stay.

A control sample of other high-impact articles is asked too and only reported,
to check that the change leaves the rest of the scale alone.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom.contracts import TriageResult
from daily_insights_api.modules.newsroom.providers import CallAudit, JsonModel
from daily_insights_api.modules.newsroom.replay.costs import DEEPSEEK_FLASH, MILLION, ChatPrice

# Energy, shipping and attack wording in Chinese and English titles.
ENERGY_TITLE_PATTERN = (
    r"能源|原油|石油|油價|油輪|油田|油氣|煉油|天然氣|LNG|布蘭特|WTI|OPEC|"
    r"航運|航道|航商|商船|貨輪|貨櫃船|船隻|船舶|海峽|荷姆茲|荷莫茲|霍爾木茲|曼德|紅海|蘇伊士|"
    r"胡塞|青年運動|攻擊|襲擊|遇襲|空襲|飛彈|導彈|無人機|封鎖|最後通牒|"
    r"Hormuz|Mandeb|Red Sea|Suez|Houthi|oil|crude|Brent|tanker|shipping|vessel|strait|"
    r"attack|strike|missile|drone|blockade|ultimatum"
)
ENERGY_TOPICS = ("commodities",)


@dataclass(frozen=True, slots=True)
class Selection:
    """Which articles a targeted re-triage asks again (all must already be relevant).

    A target article has ``impact >= min_impact`` and is about energy or
    shipping: one of ``topics``, ``escalation >= min_escalation``, or a title
    matching ``title_pattern``. The control sample takes up to
    ``control_sample`` other relevant articles with an impact in
    ``control_impacts`` per run, in a fixed order (a hash of the article id).
    """

    min_impact: int = 3
    topics: tuple[str, ...] = ENERGY_TOPICS
    min_escalation: int = 1
    title_pattern: str = ENERGY_TITLE_PATTERN
    control_sample: int = 50
    control_impacts: tuple[int, ...] = (4, 5)

    def describe(self) -> str:
        return (
            f"relevant 且 impact ≥ {self.min_impact}, 並且 (主題屬 {'/'.join(self.topics)}"
            f" 或 escalation ≥ {self.min_escalation} 或標題符合能源/航運/攻擊字詞);"
            f" 對照樣本: 其餘 relevant、impact ∈ {set(self.control_impacts)} 的文章,"
            f" 依文章 id 雜湊固定順序取 {self.control_sample} 篇 (只報告不套用)"
        )


@dataclass(frozen=True, slots=True)
class Judged:
    """One window article as the base triage left it."""

    article_id: uuid.UUID
    edition_date: date
    title: str
    topic: str | None
    story_type: str | None
    signals: dict[str, Any]
    relevant: bool
    event_id: uuid.UUID | None

    @property
    def impact(self) -> int | None:
        value = self.signals.get("impact")
        return value if isinstance(value, int) else None

    @property
    def escalation(self) -> int:
        value = self.signals.get("escalation", 0)
        return value if isinstance(value, int) else 0


def is_target(article: Judged, selection: Selection) -> bool:
    if not article.relevant or article.impact is None or article.impact < selection.min_impact:
        return False
    return (
        article.topic in selection.topics
        or article.escalation >= selection.min_escalation
        or re.search(selection.title_pattern, article.title, re.IGNORECASE) is not None
    )


def _order(article_id: uuid.UUID) -> str:
    return hashlib.sha256(str(article_id).encode()).hexdigest()


def select(articles: Iterable[Judged], selection: Selection) -> tuple[list[Judged], list[Judged]]:
    """The targets (in the given order) and the control sample of a run's articles."""
    rows = list(articles)
    targets = [article for article in rows if is_target(article, selection)]
    chosen = {article.article_id for article in targets}
    others = sorted(
        (
            article
            for article in rows
            if article.article_id not in chosen
            and article.relevant
            and article.impact in selection.control_impacts
        ),
        key=lambda article: _order(article.article_id),
    )
    return targets, others[: selection.control_sample]


def merged_result(base: TriageResult, answer: TriageResult) -> TriageResult:
    """The base judgement with the re-judged scale fields of ``answer``."""
    return base.model_copy(
        update={
            "impact": answer.impact,
            "escalation": answer.escalation,
            "story_type": answer.story_type,
            "new_facts": answer.new_facts,
            "event_date": answer.event_date,
        }
    )


@dataclass(frozen=True, slots=True)
class Request:
    system: str
    payload: dict[str, Any]
    model: str


class RecordingModel:
    """A ``JsonModel`` that remembers every triage request it passes on, by article."""

    def __init__(self, inner: JsonModel) -> None:
        self._inner = inner
        self.requests: dict[uuid.UUID, Request] = {}

    async def complete[R: BaseModel](
        self,
        database: AsyncSession,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        result_type: type[R],
        audit: CallAudit,
    ) -> R:
        if audit.stage == "triage" and audit.subject_id is not None:
            self.requests[audit.subject_id] = Request(system, payload, model)
        return await self._inner.complete(
            database,
            model=model,
            system=system,
            payload=payload,
            result_type=result_type,
            audit=audit,
        )


@dataclass
class Estimate:
    """Tokens and USD a targeted re-triage would spend, before it runs."""

    calls: int = 0
    input_tokens: float = 0.0
    system_tokens: float = 0.0
    output_tokens: float = 0.0
    per_day: dict[str, int] = field(default_factory=dict)

    def add(
        self,
        day: date,
        requests: Sequence[Request],
        *,
        system: str,
        tokens_per_char: float,
        output_per_call: float,
    ) -> None:
        self.per_day[day.isoformat()] = len(requests)
        for request in requests:
            payload = json.dumps(request.payload, ensure_ascii=False, separators=(",", ":"))
            self.calls += 1
            self.input_tokens += (len(system) + len(payload)) * tokens_per_char
            self.system_tokens += len(system) * tokens_per_char
            self.output_tokens += output_per_call

    def usd(self, price: ChatPrice = DEEPSEEK_FLASH, *, system_cached: bool = True) -> float:
        """Peak price; with ``system_cached`` the shared system prompt is a cache hit."""
        hit = self.system_tokens if system_cached else 0.0
        return (
            hit * price.cache_hit
            + (self.input_tokens - hit) * price.cache_miss
            + self.output_tokens * price.output
        ) / MILLION

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "per_day": self.per_day,
            "input_tokens": round(self.input_tokens),
            "system_tokens": round(self.system_tokens),
            "output_tokens": round(self.output_tokens),
            "usd_peak_system_cached": round(self.usd(), 4),
            "usd_peak_no_cache": round(self.usd(system_cached=False), 4),
        }
