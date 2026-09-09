import asyncio
from argparse import Namespace
from datetime import date
from pathlib import Path as FileSystemPath
from typing import Any, cast

import pytest
from anyio import Path
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.news.feeds import effective_hostnames
from daily_insights_api.modules.news.llm import DeepSeekClient
from daily_insights_api.modules.reports.scheduler import parse_args
from daily_insights_api.scripts import run_daily_news, run_daily_news_scheduler


def _settings(**overrides: object) -> Settings:
    base = Settings(
        environment="development",
        database_url="postgresql+psycopg://user:pass@localhost/database",
        session_secret=SecretStr("test-session-secret" * 3),
        password_pepper=SecretStr("test-password-pepper" * 3),
    )
    return base.model_copy(update=overrides)


def test_daily_news_cli_shares_the_scheduler_argument_contract() -> None:
    args = parse_args(
        ["--once", "--edition-date", "2026-09-02", "--market", "us_equity"],
        description="daily news",
        configure=run_daily_news.configure_arguments,
    )
    assert args.once is True
    assert args.edition_date == date(2026, 9, 2)
    assert args.market == "us_equity"
    with pytest.raises(SystemExit):
        parse_args(["--market", "fx"], configure=run_daily_news.configure_arguments)
    assert run_daily_news.RETRY_POLICY.retries("unavailable")
    assert run_daily_news.RETRY_POLICY.retries("failed")
    assert run_daily_news.RETRY_POLICY.retries("partial")


@pytest.mark.parametrize("key", ["CHANGE_ME_MODEL_API_KEY", "   \t", None])
async def test_daily_news_cli_rejects_unusable_key_before_building_client(
    monkeypatch: pytest.MonkeyPatch, key: str | None, tmp_path: FileSystemPath
) -> None:
    settings = _settings(
        daily_news_enabled=True,
        model_api_key=SecretStr(key) if key is not None else None,
    )
    monkeypatch.setattr(run_daily_news, "get_settings", lambda: settings)
    monkeypatch.setattr(run_daily_news, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    monkeypatch.setattr(
        run_daily_news,
        "parse_args",
        lambda **_: Namespace(once=True, edition_date=date(2026, 9, 2)),
    )

    class UnexpectedClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("the model client must not be built for a placeholder key")

    monkeypatch.setattr(run_daily_news, "DeepSeekClient", UnexpectedClient)

    with pytest.raises(SystemExit, match="model API key is required"):
        await run_daily_news.main()


async def test_daily_news_cli_builds_the_model_client_with_the_configured_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: FileSystemPath
) -> None:
    settings = _settings(
        daily_news_enabled=True,
        model_api_key=SecretStr("real-model-key"),
        model_timeout_seconds=90,
    )
    monkeypatch.setattr(run_daily_news, "get_settings", lambda: settings)
    monkeypatch.setattr(run_daily_news, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    monkeypatch.setattr(
        run_daily_news,
        "parse_args",
        lambda **_: Namespace(once=True, edition_date=date(2026, 9, 2)),
    )
    built: list[dict[str, object]] = []

    class RecordingClient:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)

        async def aclose(self) -> None:
            return None

    class FakeEngine:
        async def dispose(self) -> None:
            return None

    def stop(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise asyncio.CancelledError

    monkeypatch.setattr(run_daily_news, "DeepSeekClient", RecordingClient)
    monkeypatch.setattr(run_daily_news, "create_engine", lambda _: FakeEngine())
    monkeypatch.setattr(run_daily_news, "create_session_factory", lambda _: None)
    monkeypatch.setattr(run_daily_news, "build_runner", stop)

    with pytest.raises(asyncio.CancelledError):
        await run_daily_news.main()
    assert built[0]["timeout_seconds"] == 90
    assert built[0]["model"] == settings.model_name


async def test_disabled_daily_news_only_maintains_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: FileSystemPath
) -> None:
    heartbeat_path = tmp_path / "heartbeat"
    monkeypatch.setattr(run_daily_news, "get_settings", lambda: _settings(daily_news_enabled=False))
    monkeypatch.setattr(run_daily_news, "HEARTBEAT_PATH", str(heartbeat_path))
    monkeypatch.setattr(
        run_daily_news, "parse_args", lambda **_: Namespace(once=False, edition_date=None)
    )
    observed: list[Path] = []

    async def stop(heartbeat: Path) -> None:
        observed.append(heartbeat)
        raise asyncio.CancelledError

    monkeypatch.setattr(run_daily_news, "maintain_disabled_heartbeat", stop)

    with pytest.raises(asyncio.CancelledError):
        await run_daily_news.main()
    assert observed == [Path(heartbeat_path)]
    assert heartbeat_path.exists()


async def test_runner_returns_edition_status_and_refreshes_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: FileSystemPath
) -> None:
    heartbeat = Path(tmp_path / "heartbeat")
    settings = _settings(daily_news_enabled=True, news_extra_hostnames="www.reuters.com")
    seen: list[tuple[str, date, frozenset[str], float, float, str | None]] = []

    async def fake_run_all_editions(
        session_factory: object,
        client: object,
        edition_date: date,
        *,
        allowed_hostnames: frozenset[str],
        fetch_timeout_seconds: float,
        discovery_timeout_seconds: float,
    ) -> str:
        del session_factory, client
        seen.append(
            (
                "all",
                edition_date,
                allowed_hostnames,
                fetch_timeout_seconds,
                discovery_timeout_seconds,
                None,
            )
        )
        return "unavailable"

    async def fake_run_news_edition(
        session_factory: object,
        client: object,
        edition_date: date,
        *,
        allowed_hostnames: frozenset[str],
        fetch_timeout_seconds: float,
        discovery_timeout_seconds: float,
        spec: object,
    ) -> str:
        del session_factory, client
        seen.append(
            (
                "one",
                edition_date,
                allowed_hostnames,
                fetch_timeout_seconds,
                discovery_timeout_seconds,
                getattr(spec, "market_code", None),
            )
        )
        return "partial"

    monkeypatch.setattr(run_daily_news, "run_all_editions", fake_run_all_editions)
    monkeypatch.setattr(run_daily_news, "run_news_edition", fake_run_news_edition)
    factory = cast(async_sessionmaker[AsyncSession], object())
    client = cast(DeepSeekClient, object())
    every = run_daily_news.build_runner(factory, client, settings, heartbeat)
    single = run_daily_news.build_runner(factory, client, settings, heartbeat, market="tw_equity")

    assert await every(date(2026, 9, 2)) == "unavailable"
    assert await single(date(2026, 9, 2)) == "partial"
    assert seen == [
        (
            "all",
            date(2026, 9, 2),
            effective_hostnames("www.reuters.com"),
            settings.news_fetch_timeout_seconds,
            settings.news_discovery_timeout_seconds,
            None,
        ),
        (
            "one",
            date(2026, 9, 2),
            effective_hostnames("www.reuters.com"),
            settings.news_fetch_timeout_seconds,
            settings.news_discovery_timeout_seconds,
            "tw_equity",
        ),
    ]
    assert settings.news_discovery_timeout_seconds == 30
    assert await heartbeat.exists()


async def test_queue_scheduler_uses_the_no_catchup_schedule_and_enqueues_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: FileSystemPath
) -> None:
    scheduled: list[date] = []
    scheduler_options: dict[str, object] = {}

    class Engine:
        async def dispose(self) -> None:
            return None

    async def enqueue(_: object, *, edition_date: date) -> str:
        scheduled.append(edition_date)
        return "queued"

    async def drive(runner: object, **kwargs: object) -> None:
        scheduler_options.update(kwargs)
        await cast(Any, runner)(date(2026, 9, 8))
        raise asyncio.CancelledError

    monkeypatch.setattr(
        run_daily_news_scheduler,
        "get_daily_news_scheduler_settings",
        lambda: _settings(daily_news_enabled=True),
    )
    monkeypatch.setattr(run_daily_news_scheduler, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    monkeypatch.setattr(
        run_daily_news_scheduler,
        "parse_args",
        lambda **_: Namespace(once=False, edition_date=None),
    )
    monkeypatch.setattr(run_daily_news_scheduler, "create_engine", lambda _: Engine())
    monkeypatch.setattr(run_daily_news_scheduler, "create_session_factory", lambda _: object())
    monkeypatch.setattr(run_daily_news_scheduler, "enqueue_automatic_news_all_run", enqueue)
    monkeypatch.setattr(run_daily_news_scheduler, "run_scheduler", drive)

    with pytest.raises(asyncio.CancelledError):
        await run_daily_news_scheduler.main()

    assert scheduled == [date(2026, 9, 8)]
    assert scheduler_options["catch_up_on_start"] is False
    assert callable(scheduler_options["now"])
    retry = cast(Any, scheduler_options["retry"])
    assert retry.interval.total_seconds() == 30 * 60
    assert retry.retries("failed")
