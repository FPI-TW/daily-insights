from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.core.security import generate_session_token, hash_token
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session


def create_session(
    database: AsyncSession,
    *,
    user: User,
    settings: Settings,
) -> tuple[Session, str, str]:
    assert settings.session_secret is not None
    secret = settings.session_secret.get_secret_value()
    token = generate_session_token()
    csrf_token = generate_session_token()
    now = datetime.now(UTC)
    session = Session(
        user_id=user.id,
        token_hash=hash_token(token, secret),
        csrf_token_hash=hash_token(csrf_token, secret),
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
        last_seen_at=now,
    )
    database.add(session)
    return session, token, csrf_token


def rotate_csrf_token(session: Session, settings: Settings) -> str:
    """Replace the per-session CSRF secret and return it exactly once."""
    assert settings.session_secret is not None
    csrf_token = generate_session_token()
    session.csrf_token_hash = hash_token(
        csrf_token,
        settings.session_secret.get_secret_value(),
    )
    return csrf_token
