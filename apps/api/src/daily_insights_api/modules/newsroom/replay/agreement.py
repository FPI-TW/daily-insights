"""Triage agreement: production's judgements asked again of another provider.

Switching providers for the same model (DeepSeek's ``deepseek-chat`` to
OpenCode Go's ``deepseek-v4.1-flash``) should leave triage where it was. This
module checks that article by article, without replaying whole windows:

``sample``  picks articles production triaged, stratified by edition day,
            relevance, topic and language, and rebuilds the exact request each
            one was triaged with: the baseline prompt, the article as stored,
            and the candidate events of its window at the moment production
            asked (events opened, and members assigned, before that call; the
            same nearest-event search and representative titles, computed in
            production's own database, read-only).
``ask``     sends those requests to the provider as repetition ``rep``. Every
            answer is cached under the provider's tag, every live call priced
            into the round's ledger, and the run stops before the budget.
``report``  compares the answers with production and with each other.

Production's call time is its audit row's ``created_at``, which Postgres sets
to the start of the triage transaction (the same instant the event an article
opens is stamped with), so "before that call" is exact up to the commits of
calls that finished within the same milliseconds. Production's prompt tokens
are compared with the provider's as a check that the rebuilt request is the
one production sent.
"""

import asyncio
import hashlib
import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom import clock, scoring, triage
from daily_insights_api.modules.newsroom.clustering import CANDIDATE_EVENT_LIMIT, CandidateEvent
from daily_insights_api.modules.newsroom.contracts import EventMatch, EventNew, TriageResult
from daily_insights_api.modules.newsroom.models import MARKET_CODES
from daily_insights_api.modules.newsroom.providers import (
    CallAudit,
    JsonModel,
    OpenAICompatibleJsonModel,
)
from daily_insights_api.modules.newsroom.queue import FatalStageError, RetryableStageError
from daily_insights_api.modules.newsroom.replay.cache import (
    ApiFailures,
    CachedJsonModel,
    CacheStats,
    ResponseCache,
    replay_rep,
    request_key,
    watch_usage,
)
from daily_insights_api.modules.newsroom.replay.costs import (
    Ledger,
    LedgerEntry,
    ledger_total,
    read_ledger,
)
from daily_insights_api.modules.newsroom.replay.provider import LlmProvider
from daily_insights_api.modules.newsroom.replay.runner import HOLDOUT
from daily_insights_api.modules.newsroom.replay.snapshot import ReplayError, SourceDatabase
from daily_insights_api.modules.newsroom.variants import BASELINE

REPRESENTATIVE_TITLES = 2
ABORTING_FATAL_CODES = ("_http_401", "_http_402", "_http_403")
LLM_TIMEOUT_SECONDS = 120.0


# --- Sample ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sampled:
    """One article as production triaged it, and the request it was triaged with."""

    article_id: str
    edition_date: str
    title: str
    language: str
    stratum: str
    relevant: bool
    topic: str | None
    story_type: str | None
    signals: dict[str, Any]
    # "new", "match:<event id>", None when not relevant, "unknown" when the rebuilt
    # candidates cannot show what production chose (see ``production_choice``).
    choice: str | None
    input_tokens: int | None
    payload: dict[str, Any]
    candidate_ids: list[str] = field(default_factory=list)
    merge_affected: bool = False


def language(title: str) -> str:
    """``zh`` when the title has a CJK character, else ``other`` (triage translates those)."""
    return "other" if triage.needs_translation(title) else "zh"


def stratum(row: Mapping[str, Any]) -> str:
    relevant = "relevant" if row["relevant"] else "irrelevant"
    return f"{row['edition_date']}|{relevant}|{row['topic'] or '-'}|{language(row['title'])}"


def _order(article_id: str) -> str:
    return hashlib.sha256(article_id.encode()).hexdigest()


def allocate(sizes: Mapping[str, int], total: int) -> dict[str, int]:
    """Per-stratum sample sizes: at least one from every stratum, the rest proportional
    (largest remainder), never more than a stratum holds."""
    strata = sorted(name for name, size in sizes.items() if size > 0)
    if total < len(strata):
        raise ValueError(f"{total} articles cannot cover {len(strata)} strata")
    chosen = dict.fromkeys(strata, 1)
    population = sum(sizes[name] for name in strata)
    remaining = total - len(strata)
    shares = {name: remaining * sizes[name] / population for name in strata}
    for name in strata:
        chosen[name] = min(sizes[name], chosen[name] + int(shares[name]))
    left = total - sum(chosen.values())
    order = sorted(strata, key=lambda name: (-(shares[name] % 1), name))
    while left > 0 and any(chosen[name] < sizes[name] for name in strata):
        for name in order:
            if left == 0:
                break
            if chosen[name] < sizes[name]:
                chosen[name] += 1
                left -= 1
    return chosen


def draw(rows: Sequence[Mapping[str, Any]], size: int) -> list[Mapping[str, Any]]:
    """A stratified sample of ``rows`` in a fixed order (a hash of the article id)."""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[stratum(row)].append(row)
    counts = allocate({name: len(members) for name, members in groups.items()}, size)
    picked: list[Mapping[str, Any]] = []
    for name in sorted(counts):
        members = sorted(groups[name], key=lambda row: _order(str(row["id"])))
        picked += members[: counts[name]]
    return picked


def production_choice(
    row: Mapping[str, Any], candidate_ids: Sequence[str], merged: Mapping[str, str]
) -> str | None:
    """What production's triage answered about the event, read back from the database.

    An article that opened its event (the event is stamped with the article's
    call time), joined one opened while its call was in flight, or was held
    back from opening one (relevant, no event) answered "new"; an article in an
    event that existed when it was asked matched that event, which must then be
    among the rebuilt candidates. A merge moves articles, so an article in an
    event that absorbed another cannot be traced and is "unknown".
    """
    if not row["relevant"]:
        return None
    event_id = row["event_id"]
    if event_id is None or row["opened"]:
        return "new"
    if event_id in merged.values():
        return "unknown"
    if row["event_created_at"] is not None and row["event_created_at"] > row["called_at"]:
        return "new"
    return f"match:{event_id}" if event_id in candidate_ids else "unknown"


ROWS_SQL = """
SELECT a.id::text AS id, a.edition_date::text AS edition_date, a.title, a.feed_summary,
       a.body_status,
       CASE WHEN a.body_status = 'ok' THEN left(a.body, {excerpt}) END AS body_excerpt,
       a.relevant, a.topic, a.story_type, a.triage_signals, a.event_id::text AS event_id,
       s.name AS source_name, s.trust_tier, c.created_at AS called_at,
       c.input_tokens, e.created_at AS event_created_at,
       coalesce(e.created_at = c.created_at, false) AS opened
FROM newsroom_articles a
JOIN newsroom_sources s ON s.id = a.source_id
JOIN LATERAL (
    SELECT created_at, input_tokens FROM newsroom_llm_calls
    WHERE stage = 'triage' AND subject_id = a.id AND error_code IS NULL
    ORDER BY created_at DESC LIMIT 1
) c ON true
LEFT JOIN newsroom_events e ON e.id = a.event_id
WHERE a.edition_date BETWEEN '{start}' AND '{end}' AND a.triage_status = 'done'
"""

# ``clustering.nearest_events`` and ``representative_titles`` as they answered at
# the call: only members whose triage had committed before it count.
CANDIDATES_SQL = """
WITH asked AS (
    SELECT a.id, a.edition_date, a.embedding, c.created_at AS t
    FROM newsroom_articles a
    JOIN LATERAL (
        SELECT created_at FROM newsroom_llm_calls
        WHERE stage = 'triage' AND subject_id = a.id AND error_code IS NULL
        ORDER BY created_at DESC LIMIT 1
    ) c ON true
    WHERE a.id = ANY('{{{ids}}}'::uuid[]) AND a.embedding IS NOT NULL
)
SELECT asked.id::text AS article_id, near.rank, near.event_id::text AS event_id,
       near.working_title,
       (SELECT coalesce(json_agg(ranked.title ORDER BY ranked.position), '[]'::json) FROM (
            SELECT m.title, row_number() OVER (
                ORDER BY src.trust_tier DESC, m.first_seen_at, m.id) AS position
            FROM newsroom_articles m JOIN newsroom_sources src ON src.id = m.source_id
            WHERE m.event_id = near.event_id AND m.triaged_at < asked.t
        ) ranked WHERE ranked.position <= {titles}) AS titles
FROM asked CROSS JOIN LATERAL (
    SELECT e.id AS event_id, e.working_title,
           row_number() OVER (ORDER BY min(m.embedding <=> asked.embedding), e.id) AS rank
    FROM newsroom_events e JOIN newsroom_articles m ON m.event_id = e.id
    WHERE e.edition_date = asked.edition_date AND m.edition_date = asked.edition_date
      AND m.embedding IS NOT NULL AND m.id <> asked.id AND m.triaged_at < asked.t
    GROUP BY e.id, e.working_title
    ORDER BY min(m.embedding <=> asked.embedding), e.id
    LIMIT {limit}
) near
ORDER BY asked.id, near.rank
"""


def _check_range(start: date, end: date) -> None:
    if start <= HOLDOUT[1] and end >= HOLDOUT[0]:
        raise ReplayError(f"{start}..{end} overlaps the hold-out period {HOLDOUT[0]}..{HOLDOUT[1]}")


def _timestamp(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def build_sample(
    source: SourceDatabase, *, start: date, end: date, size: int, batch: int = 50
) -> list[Sampled]:
    """Draw the sample from production (read-only) and rebuild each request."""
    _check_range(start, end)
    source.assert_read_only()
    rows = source.rows(
        ROWS_SQL.format(start=start, end=end, excerpt=triage.BODY_EXCERPT_CHARS).strip()
    )
    for row in rows:
        row["called_at"] = _timestamp(row["called_at"])
        row["event_created_at"] = _timestamp(row["event_created_at"])
    merged = {
        row["id"]: row["merged_into_id"]
        for row in source.rows(
            "SELECT id::text AS id, merged_into_id::text AS merged_into_id FROM newsroom_events "
            f"WHERE status = 'merged' AND edition_date BETWEEN '{start}' AND '{end}'"
        )
    }
    picked = draw(rows, size)
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for offset in range(0, len(picked), batch):
        ids = ",".join(str(row["id"]) for row in picked[offset : offset + batch])
        for row in source.rows(
            CANDIDATES_SQL.format(
                ids=ids, titles=REPRESENTATIVE_TITLES, limit=CANDIDATE_EVENT_LIMIT
            ).strip()
        ):
            candidates[row["article_id"]].append(row)
    involved = set(merged) | set(merged.values())
    sample: list[Sampled] = []
    for chosen in picked:
        events = sorted(candidates[chosen["id"]], key=lambda item: int(item["rank"]))
        candidate_ids = [event["event_id"] for event in events]
        payload = triage.triage_payload(
            title=chosen["title"],
            source_name=chosen["source_name"],
            trust_tier=int(chosen["trust_tier"]),
            summary=chosen["feed_summary"],
            body_excerpt=chosen["body_excerpt"],
            candidates=[
                CandidateEvent(
                    id=uuid.UUID(event["event_id"]),
                    working_title=event["working_title"],
                    similarity=0.0,
                    titles=tuple(event["titles"] or ()),
                )
                for event in events
            ],
        )
        sample.append(
            Sampled(
                article_id=chosen["id"],
                edition_date=chosen["edition_date"],
                title=chosen["title"],
                language=language(chosen["title"]),
                stratum=stratum(chosen),
                relevant=bool(chosen["relevant"]),
                topic=chosen["topic"],
                story_type=chosen["story_type"],
                signals=dict(chosen["triage_signals"] or {}),
                choice=production_choice(chosen, candidate_ids, merged),
                input_tokens=chosen["input_tokens"],
                payload=payload,
                candidate_ids=candidate_ids,
                merge_affected=bool(involved & {*candidate_ids, chosen["event_id"]}),
            )
        )
    return sample


def write_sample(path: Path, sample: Sequence[Sampled], meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"meta": meta, "articles": [asdict(item) for item in sample]},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


def read_sample(path: Path) -> tuple[dict[str, Any], list[Sampled]]:
    if not path.exists():
        raise ReplayError(f"{path} does not exist; run agreement-sample first")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["meta"], [Sampled(**row) for row in data["articles"]]


# --- Ask -------------------------------------------------------------------------------


class _Session:
    """What the cached client touches on a session: ``add``, ``add_all`` and ``new``."""

    def __init__(self) -> None:
        self.new: list[Any] = []

    def add(self, row: Any) -> None:
        self.new.append(row)

    def add_all(self, rows: Iterable[Any]) -> None:
        self.new.extend(rows)


@dataclass
class Answer:
    result: dict[str, Any] | None = None
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class BudgetReached(ReplayError):
    """The round's ledger reached the spending limit; nothing more was asked."""


def live_client(provider: LlmProvider) -> Callable[[], JsonModel]:
    def build() -> JsonModel:
        model = OpenAICompatibleJsonModel(
            base_url=provider.base_url,
            api_key=provider.read_key(),
            timeout_seconds=LLM_TIMEOUT_SECONDS,
            user_agent=provider.user_agent,
            session_header=provider.session_header,
            extra_body=dict(provider.extra_body),
        )
        watch_usage(model)
        return model

    return build


async def ask(
    sample: Sequence[Sampled],
    *,
    cache: ResponseCache,
    ledger: Ledger,
    provider: LlmProvider,
    rep: int,
    budget_usd: float,
    concurrency: int = 4,
    live: Callable[[], JsonModel] | None = None,
) -> dict[str, Answer]:
    """Each sampled request asked as repetition ``rep``; stops at ``budget_usd`` spent."""
    prompt = triage.load_triage_prompt(variant=BASELINE)
    stats = CacheStats()
    tag = provider.cache_tag()
    model = CachedJsonModel(
        cache,
        stats,
        live=live or live_client(provider),
        ledger=ledger,
        failures=ApiFailures(),
        provider=tag,
    )
    spent_before = ledger_total(read_ledger(ledger.path))["usd"]
    semaphore = asyncio.Semaphore(concurrency)
    answers: dict[str, Answer] = {}
    stop: list[str] = []

    async def one(item: Sampled) -> None:
        async with semaphore:
            if stop:
                return
            if spent_before + stats.live_usd() >= budget_usd:
                stop.append(f"budget {budget_usd} reached")
                return
            subject = uuid.UUID(item.article_id)
            answer = Answer()
            try:
                result = await model.complete(
                    cast(AsyncSession, _Session()),
                    model=provider.model,
                    system=prompt.text,
                    payload=item.payload,
                    result_type=TriageResult,
                    audit=CallAudit("triage", subject, prompt.version),
                )
                answer.result = json.loads(result.model_dump_json(by_alias=True))
            except RetryableStageError as error:
                answer.error = error.code
            except FatalStageError as error:
                if error.code.endswith(ABORTING_FATAL_CODES):
                    stop.append(error.code)
                    return
                answer.error = error.code
            key, _ = request_key(
                model=provider.model,
                system=prompt.text,
                payload=item.payload,
                subject=subject,
                rep=rep,
                provider=tag,
            )
            cached = cache.answer(key, 0)
            if cached is not None:
                answer.input_tokens = cached.input_tokens
                answer.output_tokens = cached.output_tokens
            answers[item.article_id] = answer

    try:
        with replay_rep(rep):
            async with asyncio.TaskGroup() as group:
                for item in sample:
                    group.create_task(one(item))
    finally:
        await model.aclose()
    if stop:
        reason = stop[0]
        if reason.startswith("budget"):
            raise BudgetReached(f"{reason}; {len(answers)}/{len(sample)} answered and cached")
        raise ReplayError(f"a call failed fatally ({reason}); {len(answers)} answered")
    return answers


def write_answers(path: Path, answers: Mapping[str, Answer], meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"meta": meta, "answers": {key: asdict(value) for key, value in answers.items()}},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


def read_answers(path: Path) -> tuple[dict[str, Any], dict[str, Answer]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["meta"], {key: Answer(**value) for key, value in data["answers"].items()}


# --- Compare ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Judgement:
    """The fields compared, from production's stored triage or from an answer."""

    relevant: bool
    topic: str | None
    story_type: str | None
    impact: int | None
    new_facts: bool | None
    escalation: int
    directness: dict[str, int]
    market_relevant: dict[str, bool]
    event: str | None


def production_judgement(item: Sampled) -> Judgement:
    signals = item.signals
    directness = {market: int(value) for market, value in signals.get("directness", {}).items()}
    result = TriageResult.model_validate(
        {
            "topic": item.topic,
            "story_type": item.story_type,
            "new_facts": signals.get("new_facts"),
            "impact": signals.get("impact"),
            "directness": directness,
            "escalation": signals.get("escalation", 0),
        }
    )
    scores = scoring.market_scores(result, variant=BASELINE)
    return Judgement(
        relevant=item.relevant,
        topic=item.topic,
        story_type=item.story_type,
        impact=result.impact,
        new_facts=result.new_facts,
        escalation=result.escalation,
        directness=directness,
        market_relevant={m: scores[m] >= scoring.RELEVANT_SCORE for m in MARKET_CODES},
        event=item.choice,
    )


def answer_judgement(item: Sampled, answer: Answer) -> Judgement | None:
    """An answer scored exactly as the baseline triage handler scores it."""
    if answer.result is None:
        return None
    result = TriageResult.model_validate(answer.result)
    scores = scoring.market_scores(result, variant=BASELINE)
    relevant = scoring.is_relevant(scores)
    event: str | None = None
    if relevant:
        if isinstance(result.event, EventNew):
            event = "new"
        elif isinstance(result.event, EventMatch):
            wanted = result.event.match.strip().lower()
            event = f"match:{wanted}" if wanted in item.candidate_ids else "invalid"
        else:
            event = "invalid"
    return Judgement(
        relevant=relevant,
        topic=result.topic,
        story_type=result.story_type,
        impact=result.impact,
        new_facts=result.new_facts,
        escalation=result.escalation,
        directness=result.directness.as_dict(),
        market_relevant={m: scores[m] >= scoring.RELEVANT_SCORE for m in MARKET_CODES},
        event=event,
    )


@dataclass
class PairStats:
    """Agreement of two sides over the articles both answered."""

    articles: int = 0
    same: Counter[str] = field(default_factory=Counter)
    compared: Counter[str] = field(default_factory=Counter)
    impact_within_one: int = 0
    impact_shift: int = 0
    relevant_flips: Counter[str] = field(default_factory=Counter)
    any_difference: int = 0

    def rate(self, name: str) -> str:
        total = self.compared[name]
        return "—" if total == 0 else f"{self.same[name]}/{total} ({self.same[name] / total:.1%})"


def compare_pair(
    left: Mapping[str, Judgement], right: Mapping[str, Judgement], sample: Sequence[Sampled]
) -> PairStats:
    stats = PairStats()
    for item in sample:
        a, b = left.get(item.article_id), right.get(item.article_id)
        if a is None or b is None:
            continue
        stats.articles += 1
        differs = False

        def check(name: str, x: object, y: object) -> None:
            nonlocal differs
            stats.compared[name] += 1
            if x == y:
                stats.same[name] += 1
            else:
                differs = True

        check("relevant", a.relevant, b.relevant)
        check("topic", a.topic, b.topic)
        check("story_type", a.story_type, b.story_type)
        check("impact", a.impact, b.impact)
        check("new_facts", a.new_facts, b.new_facts)
        check("escalation", a.escalation, b.escalation)
        for market in MARKET_CODES:
            check(f"directness.{market}", a.directness.get(market), b.directness.get(market))
            check(f"market_relevant.{market}", a.market_relevant[market], b.market_relevant[market])
        if a.impact is not None and b.impact is not None:
            stats.impact_within_one += abs(a.impact - b.impact) <= 1
            stats.impact_shift += b.impact - a.impact
        if a.relevant != b.relevant:
            stats.relevant_flips["R→N" if a.relevant else "N→R"] += 1
        traceable = {a.event, b.event}.isdisjoint(
            {"unknown", "invalid"}
        ) and not item.merge_affected
        if a.relevant and b.relevant and traceable:
            check("event", a.event, b.event)
        stats.any_difference += differs
    return stats


@dataclass(frozen=True, slots=True)
class Side:
    name: str
    judgements: dict[str, Judgement]
    errors: Counter[str]


def side(name: str, sample: Sequence[Sampled], answers: Mapping[str, Answer]) -> Side:
    judgements: dict[str, Judgement] = {}
    errors: Counter[str] = Counter()
    for item in sample:
        answer = answers.get(item.article_id)
        if answer is None:
            errors["not_asked"] += 1
            continue
        judged = answer_judgement(item, answer)
        if judged is None:
            errors[answer.error or "no_result"] += 1
        else:
            judgements[item.article_id] = judged
    return Side(name, judgements, errors)


def production_side(sample: Sequence[Sampled]) -> Side:
    return Side("正式", {item.article_id: production_judgement(item) for item in sample}, Counter())


# --- DeepSeek's own redraw noise (p0b re-triage, three repetitions) ---------------------

REDRAW_FIELDS = ("impact", "story_type", "new_facts", "escalation")


def redraw_noise(runs: Sequence[Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """Pairwise agreement of repeated draws of identical requests (``retriage.json`` rows).

    Each run is one repetition; rows are matched by step, article and day.
    """
    keyed = [
        {
            (row["step"], row["article_id"], row["edition_date"]): row["new"]
            for row in rows
            if row.get("new") and date.fromisoformat(row["edition_date"]) not in _holdout_days()
        }
        for rows in runs
    ]
    pairs: list[dict[str, Any]] = []
    for i in range(len(keyed)):
        for j in range(i + 1, len(keyed)):
            common = keyed[i].keys() & keyed[j].keys()
            same = {
                name: sum(keyed[i][key][name] == keyed[j][key][name] for key in common)
                for name in REDRAW_FIELDS
            }
            within = sum(
                abs(keyed[i][key]["impact"] - keyed[j][key]["impact"]) <= 1 for key in common
            )
            anything = sum(
                any(keyed[i][key][name] != keyed[j][key][name] for name in REDRAW_FIELDS)
                for key in common
            )
            pairs.append(
                {
                    "pair": f"rep{i + 1}-rep{j + 1}",
                    "articles": len(common),
                    "same": same,
                    "impact_within_one": within,
                    "any_difference": anything,
                }
            )
    return {"pairs": pairs}


def _holdout_days() -> set[date]:
    first, last = HOLDOUT
    return {date.fromordinal(day) for day in range(first.toordinal(), last.toordinal() + 1)}


# --- Report ----------------------------------------------------------------------------


def _row_names() -> list[tuple[str, str]]:
    names = [
        ("relevant", "relevant (任一市場 ≥ 40)"),
        ("topic", "topic"),
        ("story_type", "story_type"),
        ("impact", "impact (完全相同)"),
        ("new_facts", "new_facts"),
        ("escalation", "escalation"),
    ]
    names += [(f"directness.{m}", f"directness {m}") for m in MARKET_CODES]
    names += [(f"market_relevant.{m}", f"{m} 達門檻 (≥ 40)") for m in MARKET_CODES]
    names.append(("event", "事件選擇 (兩方都 relevant 且可追溯)"))
    return names


def pair_table(pairs: Sequence[tuple[str, PairStats]]) -> list[str]:
    lines = [
        "| 欄位 | " + " | ".join(name for name, _ in pairs) + " |",
        "| --- |" + " --- |" * len(pairs),
        "| 比較篇數 | " + " | ".join(str(stats.articles) for _, stats in pairs) + " |",
    ]
    for key, label in _row_names():
        lines.append(f"| {label} | " + " | ".join(stats.rate(key) for _, stats in pairs) + " |")
    lines.append(
        "| impact 差 ≤ 1 | "
        + " | ".join(
            f"{stats.impact_within_one}/{stats.articles}" if stats.articles else "—"
            for _, stats in pairs
        )
        + " |"
    )
    lines.append(
        "| impact 平均差 (右減左) | "
        + " | ".join(
            f"{stats.impact_shift / stats.articles:+.3f}" if stats.articles else "—"
            for _, stats in pairs
        )
        + " |"
    )
    lines.append(
        "| relevant 翻動 (左→右) | "
        + " | ".join(
            ", ".join(f"{k} {v}" for k, v in sorted(stats.relevant_flips.items())) or "0"
            for _, stats in pairs
        )
        + " |"
    )
    lines.append(
        "| 任一欄位不同的篇數 | "
        + " | ".join(f"{stats.any_difference}/{stats.articles}" for _, stats in pairs)
        + " |"
    )
    return lines


def token_check(sample: Sequence[Sampled], answers: Mapping[str, Answer]) -> list[str]:
    """Provider prompt tokens minus production's, per article: a constant means the same request."""
    offsets: Counter[int] = Counter()
    for item in sample:
        answer = answers.get(item.article_id)
        if answer is None or answer.input_tokens is None or item.input_tokens is None:
            continue
        offsets[answer.input_tokens - item.input_tokens] += 1
    total = sum(offsets.values())
    if not total:
        return ["(沒有可比較的 token 數)"]
    mode, count = offsets.most_common(1)[0]
    near = sum(n for offset, n in offsets.items() if abs(offset - mode) <= 2)
    spread = ", ".join(f"{offset:+d}: {n}" for offset, n in offsets.most_common(8))
    return [
        f"- 比對 {total} 篇: 最常見的差值 {mode:+d} tokens 有 {count} 篇 ({count / total:.1%});"
        f" 與之相差 ≤ 2 的 {near} 篇 ({near / total:.1%})。",
        f"- 差值分布 (前 8): {spread}",
    ]


def breakdown(
    sample: Sequence[Sampled],
    left: Side,
    right: Side,
    key: Callable[[Sampled], str],
    label: str,
) -> list[str]:
    groups: dict[str, list[Sampled]] = defaultdict(list)
    for item in sample:
        groups[key(item)].append(item)
    lines = [
        f"| {label} | 篇數 | relevant 一致 | topic 一致 | impact 一致 | 事件選擇一致 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in sorted(groups):
        stats = compare_pair(left.judgements, right.judgements, groups[name])
        lines.append(
            f"| {name} | {stats.articles} | {stats.rate('relevant')} | {stats.rate('topic')}"
            f" | {stats.rate('impact')} | {stats.rate('event')} |"
        )
    return lines


def _brief(judgement: Judgement) -> str:
    directness = "/".join(str(judgement.directness.get(m, "-")) for m in MARKET_CODES)
    relevant = "R" if judgement.relevant else "N"
    return (
        f"{relevant} {judgement.topic} {judgement.story_type} i{judgement.impact}"
        f" nf={'T' if judgement.new_facts else 'F'} d={directness} ev={judgement.event or '-'}"
    )


def examples(
    sample: Sequence[Sampled], sides: Mapping[str, Side], focus: str, against: Sequence[str]
) -> list[str]:
    """Articles where ``focus`` differs on relevance or impact from every side in ``against``
    while those agree with each other."""
    lines = [
        "| 日期 | 標題 | " + " | ".join(against) + f" | {focus} |",
        "| --- | --- |" + " --- |" * (len(against) + 1),
    ]
    shown = 0
    for item in sample:
        others = [sides[name].judgements.get(item.article_id) for name in against]
        mine = sides[focus].judgements.get(item.article_id)
        if mine is None or any(other is None for other in others):
            continue
        settled = cast(list[Judgement], others)
        if len({(o.relevant, o.impact) for o in settled}) != 1:
            continue
        if (mine.relevant, mine.impact) == (settled[0].relevant, settled[0].impact):
            continue
        title = item.title.replace("|", "\\|")[:60]
        lines.append(
            f"| {item.edition_date[5:]} | {title} | "
            + " | ".join(_brief(o) for o in settled)
            + f" | {_brief(mine)} |"
        )
        shown += 1
    if shown == 0:
        lines.append("| — | (沒有) |" + " — |" * (len(against) + 1))
    return lines


def cost_lines(entries: Sequence[LedgerEntry]) -> list[str]:
    groups: dict[str, list[LedgerEntry]] = defaultdict(list)
    for entry in entries:
        groups[entry.run].append(entry)
    lines = [
        "| run | 呼叫 | 輸入 tokens | 其中快取 | 輸出 tokens | 其中推理 | list_cost (USD) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run, members in groups.items():
        total = ledger_total(members)
        lines.append(
            f"| {run} | {total['calls']} | {total['input_tokens']} | {total['cache_hit_tokens']}"
            f" | {total['output_tokens']} | {total['reasoning_tokens']} | {total['usd']:.4f} |"
        )
    total = ledger_total(entries)
    lines.append(
        f"| 合計 | {total['calls']} | {total['input_tokens']} | {total['cache_hit_tokens']}"
        f" | {total['output_tokens']} | {total['reasoning_tokens']} | {total['usd']:.4f} |"
    )
    return lines


def redraw_lines(noise: Mapping[str, Any]) -> list[str]:
    lines = [
        "| 重抽配對 | 篇數 | impact | story_type | new_facts | escalation"
        " | impact 差 ≤ 1 | 任一不同 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for pair in noise["pairs"]:
        n = pair["articles"] or 1
        same = pair["same"]
        lines.append(
            f"| {pair['pair']} | {pair['articles']} | "
            + " | ".join(f"{same[name] / n:.1%}" for name in REDRAW_FIELDS)
            + f" | {pair['impact_within_one'] / n:.1%} | {pair['any_difference']}"
            f" ({pair['any_difference'] / n:.1%}) |"
        )
    return lines


def report(
    sample_meta: Mapping[str, Any],
    sample: Sequence[Sampled],
    runs: Mapping[str, tuple[dict[str, Any], dict[str, Answer]]],
    *,
    ledger: Sequence[LedgerEntry] = (),
    redraws: Mapping[str, Any] | None = None,
) -> str:
    """Markdown: request fidelity, agreement tables, breakdowns and examples."""
    sides: dict[str, Side] = {"正式": production_side(sample)}
    for name, (_, answers) in runs.items():
        sides[name] = side(name, sample, answers)
    names = list(runs)
    lines = [
        "# 初篩一致性: 正式 DeepSeek vs 替代供應商",
        "",
        f"產生時間: {datetime.now(UTC).astimezone(clock.TAIPEI).isoformat(timespec='minutes')}",
        "",
        "## 樣本",
        "",
        f"- 範圍 {sample_meta.get('start')} ~ {sample_meta.get('end')}, 抽樣 {len(sample)} 篇"
        f" (母體 {sample_meta.get('population')} 篇已初篩文章), 層 = 日期、relevant、topic、"
        "語言的組合,"
        " 每層至少 1 篇, 其餘依比例; 層內依文章 id 雜湊固定順序。",
        f"- prompt: `{sample_meta.get('prompt_version')}`"
        " (variants.BASELINE, 與正式記錄的版本相同)。",
        "- 請求: triage.triage_payload 以正式庫中的文章重建; 候選事件為正式呼叫當下"
        " (稽核列 created_at = 交易開始) 已存在的事件與已提交的成員, 以正式庫的 pgvector 距離"
        "與代表標題規則重算 (唯讀查詢)。",
        f"- relevant {sum(item.relevant for item in sample)} 篇、非 relevant"
        f" {sum(not item.relevant for item in sample)} 篇; 中文標題"
        f" {sum(item.language == 'zh' for item in sample)} 篇、其他"
        f" {sum(item.language == 'other' for item in sample)} 篇;"
        f" 受事件合併影響而事件選擇不可追溯 {sum(item.merge_affected for item in sample)} 篇;"
        f" 正式事件選擇無法由重建候選看出 {sum(item.choice == 'unknown' for item in sample)} 篇。",
        "",
        "| 回答組 | 抽樣編號 | extra body | 成功 | 失敗 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, (meta, _) in runs.items():
        provider = meta.get("provider") or {}
        errors = sides[name].errors
        lines.append(
            f"| {name} | rep {meta.get('rep')} | `{json.dumps(provider.get('extra_body', {}))}`"
            f" | {len(sides[name].judgements)} | "
            + (", ".join(f"{k} {v}" for k, v in errors.items()) or "0")
            + " |"
        )
    lines += ["", "## 請求是否與正式相同 (prompt tokens)", ""]
    for name, (_, answers) in runs.items():
        lines += [f"**{name}**", "", *token_check(sample, answers), ""]
    lines += ["## 與正式的一致性", ""]
    lines += pair_table(
        [
            (
                f"正式 vs {name}",
                compare_pair(sides["正式"].judgements, sides[name].judgements, sample),
            )
            for name in names
        ]
    )
    lines += ["", "## 替代供應商自身的一致性 (同一請求)", ""]
    pairs = [
        (f"{a} vs {b}", compare_pair(sides[a].judgements, sides[b].judgements, sample))
        for index, a in enumerate(names)
        for b in names[index + 1 :]
    ]
    lines += pair_table(pairs) if pairs else ["(只有一組回答)"]
    if redraws:
        lines += [
            "",
            "## DeepSeek 自身重抽噪音 (參考)",
            "",
            "p0b 重判 3 次 (2026-10-18~22 危機期與 10-10~13 平靜期, triage_p0b prompt, 同一請求;"
            " 只有 impact、story_type、new_facts、escalation 會重判):",
            "",
            *redraw_lines(redraws),
        ]
    if names:
        first = names[0]
        lines += ["", f"## 分層: 正式 vs {first}", ""]
        lines += breakdown(sample, sides["正式"], sides[first], lambda i: i.edition_date, "日期")
        lines += [""]
        lines += breakdown(sample, sides["正式"], sides[first], lambda i: i.language, "語言")
        lines += [""]
        lines += breakdown(
            sample,
            sides["正式"],
            sides[first],
            lambda i: "relevant" if i.relevant else "非 relevant",
            "正式判定",
        )
        lines += [""]
        lines += breakdown(sample, sides["正式"], sides[first], lambda i: i.topic or "-", "topic")
        lines += [""]
        lines += breakdown(
            sample, sides["正式"], sides[first], lambda i: i.story_type or "-", "story_type"
        )
    for name in names[2:]:
        lines += [
            "",
            f"## {name} 的 relevant 或 impact 與其他各方 (一致時) 不同的文章",
            "",
            *examples(sample, sides, name, ["正式", *names[:2]]),
        ]
    if len(names) >= 2:
        lines += [
            "",
            f"## {names[0]} 與 {names[1]} 一致、但與正式不同的文章",
            "",
            *examples(sample, sides, "正式", names[:2]),
        ]
    if ledger:
        lines += ["", "## 花費 (帳本)", "", *cost_lines(ledger)]
    return "\n".join(lines) + "\n"
