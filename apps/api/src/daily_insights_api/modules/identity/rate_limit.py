import ipaddress
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from daily_insights_api.core.config import Settings
from daily_insights_api.core.security import hash_token
from daily_insights_api.modules.identity.session_models import LoginThrottle


def client_ip(request: Request, trusted_proxy_cidrs: str) -> str:
    peer = request.client.host if request.client is not None else "unknown"
    try:
        peer_address = ipaddress.ip_address(peer)
        trusted = any(
            peer_address in ipaddress.ip_network(cidr.strip())
            for cidr in trusted_proxy_cidrs.split(",")
            if cidr.strip()
        )
    except ValueError:
        trusted = False
    if trusted:
        forwarded = request.headers.get("X-Real-IP")
        if forwarded is not None:
            try:
                return str(ipaddress.ip_address(forwarded.strip()))
            except ValueError:
                pass
    return peer


async def consume_login_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    ip_address: str,
    email: str,
) -> bool:
    assert settings.session_secret is not None
    key_hash = hash_token(
        f"{ip_address}:{email}",
        settings.session_secret.get_secret_value(),
    )
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.login_rate_limit_window_seconds)
    expired = LoginThrottle.window_started_at <= cutoff
    statement = (
        insert(LoginThrottle)
        .values(
            key_hash=key_hash,
            attempt_count=1,
            window_started_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[LoginThrottle.key_hash],
            set_={
                "attempt_count": case(
                    (expired, 1),
                    else_=LoginThrottle.attempt_count + 1,
                ),
                "window_started_at": case(
                    (expired, now),
                    else_=LoginThrottle.window_started_at,
                ),
                "updated_at": now,
            },
        )
        .returning(LoginThrottle.attempt_count)
    )
    async with session_factory.begin() as database:
        await database.execute(delete(LoginThrottle).where(LoginThrottle.updated_at <= cutoff))
        count = await database.scalar(statement)
    return count is not None and count <= settings.login_rate_limit_attempts


async def clear_login_attempts(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    ip_address: str,
    email: str,
) -> None:
    assert settings.session_secret is not None
    key_hash = hash_token(
        f"{ip_address}:{email}",
        settings.session_secret.get_secret_value(),
    )
    async with session_factory.begin() as database:
        await database.execute(delete(LoginThrottle).where(LoginThrottle.key_hash == key_hash))
