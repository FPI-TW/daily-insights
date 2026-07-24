from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.identity.auth import AuthContext, require_password_changed
from daily_insights_api.modules.markets.service import (
    is_market_visible,
    visible_market_codes,
)
from daily_insights_api.modules.operations.service import LastKnownGood, get_last_known_good
from daily_insights_api.modules.reports.contracts import (
    Locale,
    PresentationContract,
    PublicationContent,
)
from daily_insights_api.modules.reports.schemas import ReportPublicationResponse
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/reports", tags=["reports"])
Member = Annotated[AuthContext, Depends(require_password_changed)]
ReportKey = Annotated[
    str,
    Query(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    ),
]


def _response(result: LastKnownGood, locale: Locale) -> ReportPublicationResponse:
    publication = result.publication
    content = PublicationContent.model_validate(publication.content)
    presentation = PresentationContract.model_validate(publication.presentations[locale])
    return ReportPublicationResponse(
        publication_id=publication.id,
        report_key=publication.report_key,
        market_code=publication.market_code,
        edition_date=publication.edition_date,
        revision=publication.revision,
        source_as_of=publication.source_as_of,
        published_at=publication.published_at,
        stale=result.freshness.status == "stale",
        stale_reason=result.freshness.stale_reason,
        content=content,
        presentation=presentation,
        locale=locale,
    )


async def _latest(
    database: AsyncSession,
    request: Request,
    *,
    report_key: str,
    market_code: str,
) -> LastKnownGood | None:
    maximum_age = timedelta(days=request.app.state.settings.report_freshness_max_age_days)
    return await get_last_known_good(
        database,
        report_key=report_key,
        market_code=market_code,
        now=datetime.now(UTC),
        maximum_age=maximum_age,
    )


@router.get("", response_model=list[ReportPublicationResponse])
async def list_latest_reports(
    request: Request,
    context: Member,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    locale: Locale = "zh-TW",
    report_key: ReportKey = "daily-market",
) -> list[ReportPublicationResponse]:
    if context.organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    responses = []
    for market_code in sorted(await visible_market_codes(database, context.organization_id)):
        result = await _latest(
            database,
            request,
            report_key=report_key,
            market_code=market_code,
        )
        if result is not None:
            responses.append(_response(result, locale))
    return responses


@router.get("/{market_code}/latest", response_model=ReportPublicationResponse)
async def get_latest_report(
    market_code: str,
    request: Request,
    context: Member,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    locale: Locale = "zh-TW",
    report_key: ReportKey = "daily-market",
) -> ReportPublicationResponse:
    if context.organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    if not await is_market_visible(database, context.organization_id, market_code):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    result = await _latest(
        database,
        request,
        report_key=report_key,
        market_code=market_code,
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    return _response(result, locale)
