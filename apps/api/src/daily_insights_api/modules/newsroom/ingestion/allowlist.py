"""Which article hosts the newsroom may fetch from.

The allowlist is every enabled source's ``hostname`` plus the
``news_extra_hostnames`` setting, minus ``news_blocked_hostnames`` (the kill
switch: blocking a host also stops polling its sources).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.ingestion.safe_http import (
    normalize_hostname,
    parse_hostnames,
)
from daily_insights_api.modules.newsroom.models import NewsroomSource

MANUAL_KIND = "manual"


def blocked_hostnames(settings: Settings) -> frozenset[str]:
    return parse_hostnames(settings.news_blocked_hostnames)


def effective_hostnames(source_hosts: set[str], settings: Settings) -> frozenset[str]:
    allowed = {normalize_hostname(host) for host in source_hosts}
    allowed |= parse_hostnames(settings.news_extra_hostnames)
    return frozenset(allowed - blocked_hostnames(settings))


async def article_allowlist(database: AsyncSession, settings: Settings) -> frozenset[str]:
    hosts = (
        await database.scalars(
            select(NewsroomSource.hostname).where(
                NewsroomSource.enabled.is_(True), NewsroomSource.kind != MANUAL_KIND
            )
        )
    ).all()
    return effective_hostnames(set(hosts), settings)
