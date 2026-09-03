"""Idempotent environment-to-model-history synchronization."""

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.model_runtime.api import CHAT_PROMPT_VERSION
from daily_insights_api.modules.model_runtime.models import (
    ActiveModelConfiguration,
    ModelConfiguration,
)


def _chat_configuration_desired(settings: Settings) -> dict[str, object]:
    return {
        "provider": settings.chat_model_provider,
        "requested_model": settings.chat_model_name,
        "prompt_version": CHAT_PROMPT_VERSION,
        "parameters": {"temperature": 0.2},
    }


async def sync_chat_model_configuration(database: AsyncSession, settings: Settings) -> None:
    """Create a new immutable version only when non-secret runtime config changes."""
    if not settings.chat_enabled:
        return
    await database.execute(text("SELECT pg_advisory_xact_lock(854921017)"))
    desired = _chat_configuration_desired(settings)
    active = await database.scalar(
        select(ModelConfiguration)
        .join(
            ActiveModelConfiguration,
            ActiveModelConfiguration.model_configuration_id == ModelConfiguration.id,
        )
        .where(ActiveModelConfiguration.singleton_id == 1)
    )
    if active is not None and all(getattr(active, key) == value for key, value in desired.items()):
        return
    version = (
        int(
            await database.scalar(select(func.coalesce(func.max(ModelConfiguration.version), 0)))
            or 0
        )
        + 1
    )
    config = ModelConfiguration(version=version, **desired)
    database.add(config)
    await database.flush()
    pointer = await database.get(ActiveModelConfiguration, 1, with_for_update=True)
    if pointer is None:
        database.add(ActiveModelConfiguration(singleton_id=1, model_configuration_id=config.id))
    else:
        pointer.model_configuration_id = config.id
