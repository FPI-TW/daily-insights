import asyncio

import pytest

import daily_insights_api.scripts.run_newsroom_worker as newsroom_worker_script
from daily_insights_api.core.config import Settings


class StopIdleLoop(Exception):
    pass


class Heartbeat:
    def __init__(self) -> None:
        self.touches = 0

    async def touch(self) -> None:
        self.touches += 1


def _forbid(*_: object, **__: object) -> object:
    raise AssertionError("a disabled newsroom worker must not open the database")


async def test_disabled_worker_idles_with_heartbeats(monkeypatch: pytest.MonkeyPatch) -> None:
    heartbeat = Heartbeat()
    sleeps: list[float] = []

    async def stop_after_second_interval(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopIdleLoop

    monkeypatch.setattr(
        newsroom_worker_script,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            environment="test",
            runtime_role="newsroom-worker",
            newsroom_enabled=False,
            newsroom_worker_poll_seconds=3,
        ),
    )
    monkeypatch.setattr(newsroom_worker_script, "HEARTBEAT_PATH", heartbeat)
    monkeypatch.setattr(newsroom_worker_script, "create_engine", _forbid)
    monkeypatch.setattr(asyncio, "sleep", stop_after_second_interval)

    with pytest.raises(StopIdleLoop):
        await newsroom_worker_script.run_worker()

    assert heartbeat.touches == 2
    assert sleeps == [3, 3]


async def test_disabled_worker_once_touches_heartbeat_and_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat = Heartbeat()
    monkeypatch.setattr(
        newsroom_worker_script,
        "get_settings",
        lambda: Settings(_env_file=None, environment="test", runtime_role="newsroom-worker"),
    )
    monkeypatch.setattr(newsroom_worker_script, "HEARTBEAT_PATH", heartbeat)
    monkeypatch.setattr(newsroom_worker_script, "create_engine", _forbid)

    await newsroom_worker_script.run_worker(once=True)

    assert heartbeat.touches == 1


async def test_worker_still_requires_its_runtime_role(monkeypatch: pytest.MonkeyPatch) -> None:
    heartbeat = Heartbeat()
    monkeypatch.setattr(
        newsroom_worker_script,
        "get_settings",
        lambda: Settings(_env_file=None, environment="test", runtime_role="orchestration-worker"),
    )
    monkeypatch.setattr(newsroom_worker_script, "HEARTBEAT_PATH", heartbeat)

    with pytest.raises(RuntimeError, match="newsroom-worker"):
        await newsroom_worker_script.run_worker(once=True)

    assert heartbeat.touches == 0


def test_main_fails_for_the_wrong_runtime_role(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        newsroom_worker_script,
        "get_settings",
        lambda: Settings(_env_file=None, environment="test", runtime_role="api"),
    )

    assert newsroom_worker_script.main() == 1
    assert "runtime role must be newsroom-worker" in capsys.readouterr().err
