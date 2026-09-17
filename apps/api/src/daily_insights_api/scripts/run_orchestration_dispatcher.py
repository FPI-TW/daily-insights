import argparse
import asyncio
from datetime import date, datetime, time
from typing import Protocol

from anyio import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Environment
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.orchestration.service import (
    TAIPEI,
    create_daily_routine,
    taipei_today,
)

HEARTBEAT_PATH = Path("/tmp/orchestration-dispatcher-heartbeat")


class DispatcherRuntimeSettings(Protocol):
    database_url: str | None
    orchestration_enabled: bool
    orchestration_activation_date: date | None


class DispatcherSettings(BaseSettings):
    """Least-privilege settings for the database-only dispatcher."""

    model_config = SettingsConfigDict(env_prefix="DAILY_INSIGHTS_", extra="ignore")

    environment: Environment = "development"
    database_url: str | None
    orchestration_enabled: bool = False
    orchestration_activation_date: date | None = None


async def dispatch_once(settings: DispatcherRuntimeSettings) -> bool:
    await HEARTBEAT_PATH.touch()
    if not settings.orchestration_enabled or settings.orchestration_activation_date is None:
        return False
    now = datetime.now(TAIPEI)
    edition = taipei_today(now)
    if edition < settings.orchestration_activation_date or now.timetz().replace(tzinfo=None) < time(
        8
    ):
        return False
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as database:
            await create_daily_routine(database, edition_date=edition)
        return True
    finally:
        await engine.dispose()


async def run_forever(settings: DispatcherRuntimeSettings) -> None:
    while True:
        await HEARTBEAT_PATH.touch()
        await dispatch_once(settings)
        await asyncio.sleep(30)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = DispatcherSettings()
    asyncio.run(dispatch_once(settings) if args.once else run_forever(settings))


if __name__ == "__main__":
    main()
