import asyncio
from datetime import date, datetime

from anyio import Path

from daily_insights_api.core.config import get_settings, is_placeholder_value
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.news.llm import DeepSeekClient
from daily_insights_api.modules.news.service import run_news_edition
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    due_edition,
    parse_args,
    run_scheduler,
)


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    heartbeat = Path("/tmp/daily-news-heartbeat")
    await heartbeat.touch()
    if not settings.daily_news_enabled and not args.once:
        while True:
            await heartbeat.touch()
            await asyncio.sleep(60)
    api_key = settings.model_api_key
    if api_key is None or is_placeholder_value(api_key.get_secret_value()):
        raise SystemExit("model API key is required for daily news")
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    client = DeepSeekClient(
        base_url=settings.model_api_base_url,
        api_key=api_key.get_secret_value(),
        model=settings.model_name,
    )

    async def runner(edition_date: date) -> None:
        try:
            await run_news_edition(
                session_factory,
                client,
                edition_date,
                allowed_hostnames=settings.news_allowed_hostnames,
                fetch_timeout_seconds=settings.news_fetch_timeout_seconds,
            )
        finally:
            await heartbeat.touch()

    try:
        now = datetime.now(TAIPEI)
        edition = args.edition_date or (now.date() if args.once else due_edition(now))
        if args.once:
            if edition is None:
                raise SystemExit("no edition is due yet; pass --edition-date")
            await runner(edition)
        else:
            await run_scheduler(runner, now=lambda: datetime.now(TAIPEI))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
