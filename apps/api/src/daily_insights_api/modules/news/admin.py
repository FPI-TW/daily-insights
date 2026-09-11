"""Back-office view of what the pipeline fetched, and manual curation.

Administrators see every candidate an edition discovered with the stage it
reached, hide a published story the model should not have picked, and queue
a manual publish of candidates it did not pick.
"""

import uuid
from collections import Counter
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.data_management.api import (
    DataManagementRunResponse,
    RunAlreadyActiveError,
    enqueue_run,
    run_response,
    taipei_today,
)
from daily_insights_api.modules.identity.api import AuthContext, require_csrf_roles, require_roles
from daily_insights_api.modules.news.editions import EDITION_ORDER, edition_spec
from daily_insights_api.modules.news.models import (
    NewsCandidate,
    NewsDependencyState,
    NewsEdition,
    NewsItem,
    NewsPresentation,
)
from daily_insights_api.modules.news.schemas import (
    NewsAdminCandidate,
    NewsAdminCounts,
    NewsAdminEdition,
    NewsAdminEditionEntry,
    NewsAdminEditionsResponse,
    NewsAdminItem,
    NewsCandidatePublishRequest,
    NewsDependencyResponse,
    NewsRecoveryResponse,
)
from daily_insights_api.modules.news.service import _lock_key
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/admin/news", tags=["news management"])
AdminRead = Annotated[AuthContext, Depends(require_roles(SystemRole.ADMIN))]
AdminWrite = Annotated[AuthContext, Depends(require_csrf_roles(SystemRole.ADMIN))]
Database = Annotated[AsyncSession, Depends(get_database_session)]
ADMIN_LOCALE = "zh-hant"
# Published stories lead, then what the model returned but the edition
# dropped, then what it reviewed and passed over, then everything it never saw.
STAGE_ORDER = {"published": 0, "dropped": 1, "reviewed": 2}


@router.get("/recovery", response_model=NewsRecoveryResponse)
async def recovery_status(_: AdminRead, database: Database) -> NewsRecoveryResponse:
    rows = (
        await database.scalars(select(NewsDependencyState).order_by(NewsDependencyState.scope))
    ).all()
    return NewsRecoveryResponse(
        dependencies=[
            NewsDependencyResponse(
                scope=row.scope,
                state=row.state,
                failure=row.failure,
                available_at=row.available_at,
                newest_article_at=row.newest_article_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]
    )


async def _latest_edition(
    database: AsyncSession, edition_date: date, market_code: str
) -> NewsEdition | None:
    return (
        await database.scalars(
            select(NewsEdition)
            .where(NewsEdition.edition_date == edition_date, NewsEdition.market_code == market_code)
            .order_by(NewsEdition.revision.desc())
            .limit(1)
        )
    ).first()


async def _is_latest_revision(database: AsyncSession, edition: NewsEdition) -> bool:
    latest = await _latest_edition(database, edition.edition_date, edition.market_code)
    return latest is not None and latest.id == edition.id


def _item_response(
    item: NewsItem, headline: str | None, candidate_id: uuid.UUID | None
) -> NewsAdminItem:
    return NewsAdminItem(
        id=item.id,
        rank=item.rank,
        origin=item.origin,
        hidden=item.hidden_at is not None,
        hidden_at=item.hidden_at,
        headline=headline or item.source_headline,
        source_headline=item.source_headline,
        source_name=item.source_name,
        source_hostname=item.source_hostname,
        source_url=item.source_url,
        source_published_at=item.source_published_at,
        topic=item.topic,
        market=item.market,
        importance=item.importance,
        event_key=item.event_key,
        candidate_id=candidate_id,
    )


def _candidate_response(candidate: NewsCandidate) -> NewsAdminCandidate:
    return NewsAdminCandidate(
        id=candidate.id,
        stage=candidate.stage,
        drop_reason=candidate.drop_reason,
        headline=candidate.headline,
        source_name=candidate.source_name,
        hostname=candidate.hostname,
        url=candidate.url,
        seen_at=candidate.seen_at,
        source_published_at=candidate.source_published_at,
        ai_rank=candidate.ai_rank,
        ai_topic=candidate.ai_topic,
        ai_market=candidate.ai_market,
        ai_importance=candidate.ai_importance,
        ai_event_key=candidate.ai_event_key,
        item_id=candidate.item_id,
        publish_run_id=candidate.publish_run_id,
        publish_requested_at=candidate.publish_requested_at,
        publish_error=candidate.publish_error,
    )


def _ordered_candidates(
    candidates: list[NewsCandidate], item_ranks: dict[uuid.UUID, int]
) -> list[NewsCandidate]:
    def sort_key(candidate: NewsCandidate) -> tuple[int, int, float, str]:
        group = STAGE_ORDER.get(candidate.stage, 3)
        within = (
            item_ranks.get(candidate.item_id or uuid.UUID(int=0), 0)
            if group == 0
            else candidate.ai_rank or 0
            if group == 1
            else 0
        )
        seen = candidate.seen_at.timestamp() if candidate.seen_at is not None else 0.0
        return (group, within, -seen, candidate.headline)

    return sorted(candidates, key=sort_key)


async def _edition_entry(
    database: AsyncSession, edition_date: date, market_code: str
) -> NewsAdminEditionEntry:
    edition = await _latest_edition(database, edition_date, market_code)
    if edition is None:
        return NewsAdminEditionEntry(market_code=market_code, edition=None, items=[], candidates=[])
    rows = (
        await database.execute(
            select(NewsItem, NewsPresentation.headline)
            .outerjoin(
                NewsPresentation,
                (NewsPresentation.item_id == NewsItem.id)
                & (NewsPresentation.locale == ADMIN_LOCALE),
            )
            .where(NewsItem.edition_id == edition.id)
            .order_by(NewsItem.rank)
        )
    ).all()
    candidates = list(
        await database.scalars(select(NewsCandidate).where(NewsCandidate.edition_id == edition.id))
    )
    candidate_by_item = {
        candidate.item_id: candidate.id for candidate in candidates if candidate.item_id is not None
    }
    items = [
        _item_response(item, headline, candidate_by_item.get(item.id)) for item, headline in rows
    ]
    stages = Counter(candidate.stage for candidate in candidates)
    counts = NewsAdminCounts(
        discovered=stages["discovered"],
        fetch_failed=stages["fetch_failed"],
        unused=stages["unused"],
        reviewed=stages["reviewed"],
        dropped=stages["dropped"],
        published=stages["published"],
        hidden=sum(1 for item in items if item.hidden),
    )
    item_ranks = {item.id: item.rank for item, _ in rows}
    return NewsAdminEditionEntry(
        market_code=market_code,
        edition=NewsAdminEdition(
            id=edition.id,
            revision=edition.revision,
            status=edition.status,
            generated_at=edition.generated_at,
            prompt_version=edition.prompt_version,
            target_items=edition_spec(market_code).target_items,
            counts=counts,
        ),
        items=items,
        candidates=[
            _candidate_response(candidate)
            for candidate in _ordered_candidates(candidates, item_ranks)
        ],
    )


@router.get("/editions", response_model=NewsAdminEditionsResponse)
async def list_editions(
    _: AdminRead,
    database: Database,
    edition_date: Annotated[date | None, Query(alias="date")] = None,
) -> NewsAdminEditionsResponse:
    """Latest revision of every market edition for one Taipei date."""
    resolved = edition_date or taipei_today()
    return NewsAdminEditionsResponse(
        edition_date=resolved,
        editions=[
            await _edition_entry(database, resolved, market_code) for market_code in EDITION_ORDER
        ],
    )


def _parse_uuid(value: str, description: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{description} not found") from None


async def _load_item_view(
    database: AsyncSession, item: NewsItem
) -> tuple[str | None, uuid.UUID | None]:
    headline = await database.scalar(
        select(NewsPresentation.headline).where(
            NewsPresentation.item_id == item.id, NewsPresentation.locale == ADMIN_LOCALE
        )
    )
    candidate_id = await database.scalar(
        select(NewsCandidate.id).where(NewsCandidate.item_id == item.id).limit(1)
    )
    return headline, candidate_id


async def _set_hidden(
    database: AsyncSession,
    item_id: str,
    *,
    hidden: bool,
    actor: AuthContext,
    request_id: str | None,
) -> NewsAdminItem:
    item = await database.get(NewsItem, _parse_uuid(item_id, "news item"))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "news item not found")
    edition = await database.get(NewsEdition, item.edition_id)
    assert edition is not None
    await database.execute(
        select(func.pg_advisory_xact_lock(_lock_key(edition.edition_date, edition.market_code)))
    )
    await database.refresh(item)
    before = {"hidden": item.hidden_at is not None}
    if (item.hidden_at is not None) != hidden:
        # Idempotent: repeating the same request neither rewrites the
        # timestamp nor records a second audit event.
        item.hidden_at = datetime.now(UTC) if hidden else None
        item.hidden_by_user_id = actor.user.id if hidden else None
        await database.execute(
            update(NewsItem)
            .where(
                NewsItem.edition_id.in_(
                    select(NewsEdition.id).where(NewsEdition.market_code == edition.market_code)
                ),
                or_(
                    NewsItem.source_url == item.source_url,
                    NewsItem.event_key == item.event_key if item.event_key else false(),
                ),
            )
            .values(hidden_at=item.hidden_at, hidden_by_user_id=item.hidden_by_user_id)
        )
        record_audit_event(
            database,
            actor_user_id=actor.user.id,
            action="news.item_hidden" if hidden else "news.item_unhidden",
            target_type="news_item",
            target_id=str(item.id),
            before=before,
            after={"hidden": hidden, "edition_id": str(item.edition_id), "rank": item.rank},
            request_id=request_id,
        )
        await database.commit()
    headline, candidate_id = await _load_item_view(database, item)
    return _item_response(item, headline, candidate_id)


@router.post("/items/{item_id}/hide", response_model=NewsAdminItem)
async def hide_item(
    item_id: str, request: Request, actor: AdminWrite, database: Database
) -> NewsAdminItem:
    return await _set_hidden(
        database, item_id, hidden=True, actor=actor, request_id=request.state.request_id
    )


@router.post("/items/{item_id}/unhide", response_model=NewsAdminItem)
async def unhide_item(
    item_id: str, request: Request, actor: AdminWrite, database: Database
) -> NewsAdminItem:
    return await _set_hidden(
        database, item_id, hidden=False, actor=actor, request_id=request.state.request_id
    )


@router.post(
    "/candidates/publish",
    response_model=DataManagementRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Edition not found."},
        status.HTTP_409_CONFLICT: {
            "description": "Edition superseded or a manual publish is already active."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "A candidate is not in the edition or is already published."
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Daily news is disabled."},
    },
)
async def publish_candidates(
    payload: NewsCandidatePublishRequest,
    request: Request,
    actor: AdminWrite,
    database: Database,
) -> DataManagementRunResponse:
    """Queue a manual publish; the worker fetches, summarises and publishes."""
    if not request.app.state.settings.daily_news_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "daily news is unavailable")
    edition = await database.get(NewsEdition, payload.edition_id)
    if edition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "news edition not found")
    if not await _is_latest_revision(database, edition):
        raise HTTPException(status.HTTP_409_CONFLICT, "edition superseded")
    candidates = list(
        await database.scalars(
            select(NewsCandidate).where(
                NewsCandidate.edition_id == edition.id,
                NewsCandidate.id.in_(payload.candidate_ids),
            )
        )
    )
    if len(candidates) != len(payload.candidate_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "candidate is not in this edition"
        )
    if any(candidate.item_id is not None for candidate in candidates):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "candidate already published")
    # The request bookkeeping rides in the run's own commit so the worker,
    # which may claim the row within a second, never sees half of it and the
    # error it writes cannot be overwritten by a later commit here. Only the
    # run id, which the worker never writes, is set afterwards.
    now = datetime.now(UTC)
    for candidate in candidates:
        candidate.publish_requested_at = now
        candidate.publish_requested_by_user_id = actor.user.id
        candidate.publish_error = None
    try:
        run = await enqueue_run(
            database,
            operation="news_publish",
            market_code=None,
            requester_id=actor.user.id,
            request_id=request.state.request_id,
            edition_date=edition.edition_date,
            payload={
                "edition_id": str(edition.id),
                "candidate_ids": [str(candidate_id) for candidate_id in payload.candidate_ids],
            },
        )
    except RunAlreadyActiveError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a manual publish is already active"
        ) from None
    for candidate in candidates:
        candidate.publish_run_id = run.id
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="news.candidate_publish_requested",
        target_type="news_edition",
        target_id=str(edition.id),
        after={
            "run_id": str(run.id),
            "market_code": edition.market_code,
            "candidate_ids": [str(candidate.id) for candidate in candidates],
        },
        request_id=request.state.request_id,
    )
    await database.commit()
    return run_response(run)
