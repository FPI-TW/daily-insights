"""Workstream ③: event analysis and per-market "why it matters" (spec §6.3).

Owner: editions worktree.

``queue.ANALYSIS`` analyses an event once: shared zh-hant headline, summary
and related symbols, plus one "why" per market edition the event sits in. The
"why" is written straight onto those items (spec §5.1). ``queue.WHY`` only
covers an item added after its event was already analysed.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.data_sources.api import TRACKED_INDICES
from daily_insights_api.modules.markets.api import TAIEX_SYMBOL
from daily_insights_api.modules.newsroom import queue
from daily_insights_api.modules.newsroom.contracts import AnalysisResult, WhyResult
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEvent,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.queue import Claim, RetryableStageError
from daily_insights_api.modules.newsroom.translation import (
    contract_error,
    live_item_condition,
    load_prompt,
    to_zh_hans,
)
from daily_insights_api.modules.newsroom.worker import Registration, Runtime, StageBinding
from daily_insights_api.modules.reports.api import (
    ACTIVE_LAUNCH_MANIFEST,
    COMMODITIES,
    FX_INSTRUMENTS,
    INSTRUMENTS,
)

ANALYSIS_PROMPT_VERSION = "newsroom.analysis.v2"
WHY_PROMPT_VERSION = "newsroom.why.v1"
MAX_ANALYSIS_ARTICLES = 5
ARTICLE_EXCERPT_CHARS = 8_000
WHY_ARTICLES = 3
WHY_EXCERPT_CHARS = 3_000

SymbolKind = Literal["index", "equity", "fx", "commodity", "rate", "crypto"]


@dataclass(frozen=True, slots=True)
class DashboardSymbol:
    """A symbol the site has a dashboard for, and the dashboard market showing it."""

    symbol: str
    kind: SymbolKind
    market_code: str


def _manifest_kind(dataset_key: str, market_code: str, symbol: str) -> SymbolKind:
    if market_code == "crypto":
        return "crypto"
    if market_code == "us_equity":
        return "equity"
    if "commodity" in dataset_key:
        return "commodity"
    return "fx" if "/" in symbol else "rate"


@cache
def symbol_catalog() -> dict[str, DashboardSymbol]:
    """Every symbol shown on a site dashboard, keyed by its upper-case symbol.

    Built from the dashboards' own definitions (launch manifest, tracked
    indices, macro dashboard instruments) so it follows them automatically.
    """
    entries: list[DashboardSymbol] = []
    dataset_market = {
        dataset_key: market.market_code
        for market in ACTIVE_LAUNCH_MANIFEST.markets
        for block in market.blocks
        for dataset_key in block.datasets
    }
    for dataset in ACTIVE_LAUNCH_MANIFEST.datasets:
        market_code = dataset_market[dataset.key]
        entries.extend(
            DashboardSymbol(symbol, _manifest_kind(dataset.key, market_code, symbol), market_code)
            for symbol in dataset.symbols
        )
    entries.extend(
        DashboardSymbol(symbol, "index", market_code)
        for symbol, market_code in TRACKED_INDICES.items()
    )
    entries.append(DashboardSymbol(TAIEX_SYMBOL, "index", "tw_equity"))
    entries.extend(
        DashboardSymbol(symbol, "commodity", "global_macro_bonds")
        for _, symbol, _, _ in COMMODITIES
    )
    entries.extend(DashboardSymbol(symbol, "fx", "forex") for _, symbol, _ in FX_INSTRUMENTS)
    entries.extend(DashboardSymbol(symbol, "index", "forex") for _, symbol, _ in INSTRUMENTS)
    catalog: dict[str, DashboardSymbol] = {}
    for entry in entries:
        catalog.setdefault(entry.symbol.upper(), entry)
    return catalog


def resolve_symbol(symbol: str) -> DashboardSymbol | None:
    """Exact catalog match, tolerating a missing ``^`` or ``/USD`` suffix."""
    catalog = symbol_catalog()
    key = symbol.strip().upper()
    for candidate in (key, f"^{key}", f"{key}/USD"):
        if candidate in catalog:
            return catalog[candidate]
    return None


def filter_related_symbols(symbols: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Keep only symbols with a site dashboard, canonicalised and de-duplicated."""
    kept: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in symbols:
        raw = item.get("symbol")
        label = item.get("label")
        if not isinstance(raw, str) or not isinstance(label, str) or not label.strip():
            continue
        entry = resolve_symbol(raw)
        if entry is None or entry.symbol in seen:
            continue
        seen.add(entry.symbol)
        kept.append({"symbol": entry.symbol, "kind": entry.kind, "label": label.strip()})
    return kept


@dataclass(frozen=True, slots=True)
class _Excerpt:
    title: str
    source: str
    body: str


async def _body_excerpts(
    database: AsyncSession, event_id: uuid.UUID, *, limit: int, chars: int
) -> list[_Excerpt]:
    """Full-text articles of the event, most trusted sources first."""
    rows = (
        await database.execute(
            select(NewsroomArticle.title, NewsroomSource.name, NewsroomArticle.body)
            .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
            .where(
                NewsroomArticle.event_id == event_id,
                NewsroomArticle.body_status == "ok",
                NewsroomArticle.body.is_not(None),
            )
            .order_by(
                NewsroomSource.trust_tier.desc(),
                NewsroomSource.weight.desc(),
                NewsroomArticle.published_at.desc().nulls_last(),
                NewsroomArticle.id,
            )
            .limit(limit)
        )
    ).all()
    return [
        _Excerpt(title=title, source=source, body=(body or "")[:chars])
        for title, source, body in rows
    ]


def _excerpt_payload(excerpts: Sequence[_Excerpt]) -> list[dict[str, str]]:
    return [{"title": e.title, "source": e.source, "body": e.body} for e in excerpts]


def _catalog_payload() -> list[dict[str, str]]:
    return [{"symbol": entry.symbol, "kind": entry.kind} for entry in symbol_catalog().values()]


async def _placed_items(
    database: AsyncSession, event_id: uuid.UUID
) -> list[tuple[NewsroomEditionItem, str]]:
    """Every item currently pointing at the event and not removed from its draft.

    This includes items moved here by an event merge and placements added in
    another market since the last analysis (spec §5.1: merge/split only
    re-queue the event, not the items). Hidden and abandoned items are kept
    too, so un-hiding never shows a "why" from an older analysis.
    """
    rows = (
        await database.execute(
            select(NewsroomEditionItem, NewsroomEdition.market_code)
            .join(NewsroomEdition, NewsroomEdition.id == NewsroomEditionItem.edition_id)
            .where(
                NewsroomEditionItem.event_id == event_id,
                NewsroomEditionItem.removed_at.is_(None),
            )
            .order_by(NewsroomEdition.market_code)
        )
    ).all()
    return [(item, market_code) for item, market_code in rows]


async def analyze_event(runtime: Runtime, database: AsyncSession, claim_: Claim) -> dict[str, Any]:
    """``queue.ANALYSIS`` handler for one event (spec §6.3 深度分析)."""
    event = await database.get(NewsroomEvent, claim_.row_id)
    if event is None:
        raise RetryableStageError("analysis_event_missing")
    if event.status == "merged":
        # The target event carries the analysis; nothing to do for this one.
        return {"analysis_status": "idle"}
    excerpts = await _body_excerpts(
        database, event.id, limit=MAX_ANALYSIS_ARTICLES, chars=ARTICLE_EXCERPT_CHARS
    )
    if not excerpts:
        # Not a failure: an admin can paste a body, which re-queues analysis (D18).
        return {"analysis_status": "needs_body"}
    items = await _placed_items(database, event.id)
    markets = sorted({market for _, market in items}) or ["global"]
    result = await runtime.llm.complete(
        database,
        model=runtime.settings.newsroom_analysis_model,
        system=load_prompt("analysis"),
        payload={
            "working_title": event.working_title,
            "markets": markets,
            "articles": _excerpt_payload(excerpts),
            "dashboard_symbols": _catalog_payload(),
        },
        result_type=AnalysisResult,
        audit=CallAudit("analysis", event.id, ANALYSIS_PROMPT_VERSION),
    )
    whys: dict[str, str] = {entry.market: entry.why for entry in result.why}
    if not set(markets) <= whys.keys():
        raise contract_error(database, "analysis_schema_invalid")
    for item, market in items:
        why = whys[market]
        await database.execute(
            update(NewsroomEditionItem)
            .where(NewsroomEditionItem.id == item.id)
            .values(
                why_zh_hant=why,
                why_zh_hans=to_zh_hans(why),
                why_status="ready",
                why_next_attempt_at=None,
                why_error_code=None,
            )
        )
    return {
        "headline_zh_hant": result.headline,
        "summary_zh_hant": result.summary,
        "headline_zh_hans": to_zh_hans(result.headline),
        "summary_zh_hans": to_zh_hans(result.summary),
        "related_symbols": filter_related_symbols(
            symbol.model_dump() for symbol in result.related_symbols
        ),
        "analysis_model": runtime.settings.newsroom_analysis_model,
        "analyzed_at": datetime.now(UTC),
    }


async def write_item_why(runtime: Runtime, database: AsyncSession, claim_: Claim) -> dict[str, Any]:
    """``queue.WHY`` handler: one market's "why" for an already analysed event."""
    row = (
        await database.execute(
            select(NewsroomEditionItem, NewsroomEdition.market_code, NewsroomEvent)
            .join(NewsroomEdition, NewsroomEdition.id == NewsroomEditionItem.edition_id)
            .join(NewsroomEvent, NewsroomEvent.id == NewsroomEditionItem.event_id)
            .where(NewsroomEditionItem.id == claim_.row_id)
        )
    ).one_or_none()
    if row is None:
        raise RetryableStageError("why_item_missing")
    item, market, event = row
    if event.analysis_status != "ready" or not event.headline_zh_hant:
        # Lost a race with a re-analysis, which writes this item's "why" itself.
        raise RetryableStageError("why_event_not_ready")
    excerpts = await _body_excerpts(database, event.id, limit=WHY_ARTICLES, chars=WHY_EXCERPT_CHARS)
    result = await runtime.llm.complete(
        database,
        model=runtime.settings.newsroom_analysis_model,
        system=load_prompt("why"),
        payload={
            "market": market,
            "headline": event.headline_zh_hant,
            "summary": event.summary_zh_hant,
            "articles": _excerpt_payload(excerpts),
        },
        result_type=WhyResult,
        audit=CallAudit("why", item.id, WHY_PROMPT_VERSION),
    )
    return {"why_zh_hant": result.why, "why_zh_hans": to_zh_hans(result.why)}


def why_claim_filter() -> Any:
    """Only claim a "why" once its event is analysed and the item is still live."""
    return (
        NewsroomEditionItem.event_id.in_(
            select(NewsroomEvent.id).where(NewsroomEvent.analysis_status == "ready")
        )
        & live_item_condition()
    )


def register(runtime: Runtime) -> Registration:
    """Stage bindings for ``queue.ANALYSIS`` and ``queue.WHY``."""

    async def analysis_handler(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
        return await analyze_event(runtime, database, claim_)

    async def why_handler(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
        return await write_item_why(runtime, database, claim_)

    return Registration(
        stages=[
            StageBinding(queue.ANALYSIS, analysis_handler),
            StageBinding(queue.WHY, why_handler, extra_filter=why_claim_filter()),
        ]
    )
