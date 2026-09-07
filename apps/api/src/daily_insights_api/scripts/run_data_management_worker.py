"""Run durable administrator-requested data reruns."""

import argparse
import asyncio

from anyio import Path

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.logging import configure_logging
from daily_insights_api.modules.data_management.service import worker_loop


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run data-management worker")
    parser.add_argument("--once", action="store_true", help="claim at most one queued run")
    args = parser.parse_args()
    configure_logging()
    settings = get_settings()
    engine = create_engine(settings)
    try:
        await worker_loop(
            create_session_factory(engine),
            settings,
            once=args.once,
            heartbeat_path=Path("/tmp/data-management-worker-heartbeat"),
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
