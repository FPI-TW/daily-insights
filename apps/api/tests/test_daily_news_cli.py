import asyncio
from argparse import Namespace
from datetime import date
from pathlib import Path as FileSystemPath
from typing import cast

import pytest
from anyio import Path
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.news.llm import DeepSeekClient
from daily_insights_api.modules.reports.scheduler import parse_args
from daily_insights_api.scripts import run_daily_news


def _settings(**overrides: object) -> Settings:
    base = Settings(
        environment="development",
        database_url="postgresql+psycopg://user:pass@localhost/database",
        session_secret=SecretStr("test-session-secret" * 3),
        password_pepper=SecretStr("test-password-pepper" * 3),
    )
    return base.model_copy(update=overrides)


def test_daily_news_cli_shares_the_scheduler_argument_contract() -> None:
    args = parse_args(["--once", "--edition-date", "2026-09-02"], description="daily news")
    assert args.once is True
    assert args.edition_date == date(2026, 9, 2)
    assert run_daily_news.RETRY_POLICY.retries("unavailable")
    assert run_daily_news.RETRY_POLICY.retries("failed")
    assert not run_daily_news.RETRY_POLICY.retries("partial")


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
    settings = _settings(daily_news_enabled=True, news_allowed_hostnames="www.reuters.com")
    seen: list[tuple[date, str, float, float]] = []

    async def fake_run_news_edition(
        session_factory: object,
        client: object,
        edition_date: date,
        *,
        allowed_hostnames: str,
        fetch_timeout_seconds: float,
        discovery_timeout_seconds: float,
    ) -> str:
        del session_factory, client
        seen.append(
            (edition_date, allowed_hostnames, fetch_timeout_seconds, discovery_timeout_seconds)
        )
        return "unavailable"

    monkeypatch.setattr(run_daily_news, "run_news_edition", fake_run_news_edition)
    runner = run_daily_news.build_runner(
        cast(async_sessionmaker[AsyncSession], object()),
        cast(DeepSeekClient, object()),
        settings,
        heartbeat,
    )

    assert await runner(date(2026, 9, 2)) == "unavailable"
    assert seen == [
        (
            date(2026, 9, 2),
            "www.reuters.com",
            settings.news_fetch_timeout_seconds,
            settings.news_discovery_timeout_seconds,
        )
    ]
    assert settings.news_discovery_timeout_seconds == 60
    assert await heartbeat.exists()
