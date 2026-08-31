from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.operations.api import LastKnownGood, get_last_known_good
from daily_insights_api.modules.reports.access import visible_report_market_codes
from daily_insights_api.modules.reports.contracts import (
    Locale,
    PresentationContract,
    PublicationContent,
)
from daily_insights_api.modules.reports.launch_manifest import LAUNCH_MARKET_ORDER
from daily_insights_api.modules.reports.schemas import ReportDetailResponse, ReportSummaryResponse
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


def _detail_response(result: LastKnownGood, locale: Locale) -> ReportDetailResponse:
    publication = result.publication
    content = PublicationContent.model_validate(publication.content)
    presentation = PresentationContract.model_validate(publication.presentations[locale])
    return ReportDetailResponse(
        publication_id=publication.id,
        report_key=publication.report_key,
        market_code=publication.market_code,
        edition_date=publication.edition_date,
        revision=publication.revision,
        source_as_of=publication.source_as_of,
        published_at=publication.published_at,
        stale=result.freshness.status == "stale",
        stale_reason=result.freshness.stale_reason,
        status=content.status,
        title=presentation.title,
        summary=presentation.summary,
        manifest_version=publication.manifest_version,
        manifest_hash=publication.manifest_hash,
        content=content,
        presentation=presentation,
        locale=locale,
    )


def _summary_response(result: LastKnownGood, locale: Locale) -> ReportSummaryResponse:
    detail = _detail_response(result, locale)
    return ReportSummaryResponse(
        **detail.model_dump(
            exclude={"content", "presentation", "manifest_version", "manifest_hash"}
        )
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


@router.get("", response_model=list[ReportSummaryResponse])
async def list_latest_reports(
    request: Request,
    context: Member,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    locale: Locale = "zh-hant",
    report_key: ReportKey = "daily-market",
) -> list[ReportSummaryResponse]:
    responses = []
    visible = await visible_report_market_codes(database, context)
    for market_code in LAUNCH_MARKET_ORDER:
        if market_code not in visible:
            continue
        result = await _latest(
            database,
            request,
            report_key=report_key,
            market_code=market_code,
        )
        if result is not None:
            responses.append(_summary_response(result, locale))
    return responses


@router.get("/{market_code}/latest", response_model=ReportDetailResponse)
async def get_latest_report(
    market_code: str,
    request: Request,
    context: Member,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    locale: Locale = "zh-hant",
    report_key: ReportKey = "daily-market",
) -> ReportDetailResponse:
    visible = await visible_report_market_codes(database, context)
    if market_code not in LAUNCH_MARKET_ORDER or market_code not in visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    result = await _latest(
        database,
        request,
        report_key=report_key,
        market_code=market_code,
    )
    if result is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {
                "code": "report_not_generated",
                "message": "report has not been generated",
            },
        )
    return _detail_response(result, locale)
