import asyncio
import sys
from pathlib import Path

from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.assets.r2.store import R2ObjectStore
from daily_insights_api.modules.podcasts.media_worker import PodcastMediaWorker


async def run_worker() -> None:
    settings = get_settings()
    if settings.runtime_role != "media-worker":
        raise RuntimeError("runtime role must be media-worker")
    if (
        settings.r2_endpoint_url is None
        or settings.r2_bucket_name is None
        or settings.r2_access_key_id is None
        or settings.r2_secret_access_key is None
    ):
        raise RuntimeError("media worker requires R2 endpoint, bucket, and scoped credentials")
    engine = create_engine(settings)
    store = R2ObjectStore.from_credentials(
        endpoint_url=settings.r2_endpoint_url,
        access_key_id=settings.r2_access_key_id.get_secret_value(),
        secret_access_key=settings.r2_secret_access_key.get_secret_value(),
    )
    try:
        worker = PodcastMediaWorker(
            create_session_factory(engine),
            store,
            settings,
            spool_directory=Path(settings.podcast_media_spool_dir),
        )
        await worker.run()
    finally:
        await engine.dispose()


def main() -> int:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f"podcast media worker failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
