"""Backfill podcast analysis for audio files that were uploaded before the
feature existed or whose analysis failed.

    python -m daily_insights_api.scripts.run_podcast_analysis [--all] [--trading-date YYYY-MM-DD]

Without --all only variants that were never analysed or failed are processed;
--all re-analyses every active file (manual titles and chapters are kept).
"""

import argparse
import asyncio
import sys
from datetime import date

from sqlalchemy import select

from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.logging import configure_logging
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.assets.r2.store import R2ObjectStore
from daily_insights_api.modules.podcasts.analysis import build_podcast_analyzer
from daily_insights_api.modules.podcasts.models import PodcastEpisode, PodcastEpisodeAudioVariant


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyse podcast audio with transcription + LLM")
    parser.add_argument("--all", action="store_true", help="re-analyse every active audio file")
    parser.add_argument(
        "--trading-date", type=date.fromisoformat, help="only this episode's trading date"
    )
    return parser.parse_args()


async def main() -> int:
    configure_logging()
    args = parse_args()
    settings = get_settings()
    if not settings.podcast_analysis_enabled:
        print("DAILY_INSIGHTS_PODCAST_ANALYSIS_ENABLED is false", file=sys.stderr)
        return 2
    if (
        settings.r2_endpoint_url is None
        or settings.r2_access_key_id is None
        or settings.r2_secret_access_key is None
    ):
        print("R2 credentials are required", file=sys.stderr)
        return 2
    store = R2ObjectStore.from_credentials(
        endpoint_url=settings.r2_endpoint_url,
        access_key_id=settings.r2_access_key_id.get_secret_value(),
        secret_access_key=settings.r2_secret_access_key.get_secret_value(),
    )
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    analyzer = build_podcast_analyzer(settings, session_factory=session_factory, store=store)
    try:
        async with session_factory() as database:
            query = (
                select(PodcastEpisodeAudioVariant.id, PodcastEpisode.trading_date)
                .join(PodcastEpisode, PodcastEpisode.id == PodcastEpisodeAudioVariant.episode_id)
                .where(PodcastEpisodeAudioVariant.is_active.is_(True))
                .order_by(PodcastEpisode.trading_date.desc())
            )
            if not args.all:
                query = query.where(
                    PodcastEpisodeAudioVariant.analysis_status.in_(["none", "failed"])
                )
            if args.trading_date is not None:
                query = query.where(PodcastEpisode.trading_date == args.trading_date)
            targets = (await database.execute(query)).all()
        emit_event("podcast.analysis.backfill.started", count=len(targets))
        outcomes: dict[str, int] = {}
        for variant_id, trading_date in targets:
            outcome = await analyzer.analyze(variant_id, actor_user_id=None)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            print(f"{trading_date} {variant_id} {outcome}")
        emit_event("podcast.analysis.backfill.finished", **outcomes)
        return 0 if outcomes.get("failed", 0) == 0 else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
