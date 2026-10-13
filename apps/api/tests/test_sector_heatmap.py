import os
import struct
import uuid
import zlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, BinaryIO, cast

import pytest
import pytest_asyncio
from conftest import remigrate_database
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import daily_insights_api.modules.orchestration.functions as orchestration_functions
from daily_insights_api.core.config import Settings
from daily_insights_api.modules.assets.object_store import ObjectMetadata, ObjectRef
from daily_insights_api.modules.data_sources.dto import DailyBar
from daily_insights_api.modules.data_sources.errors import (
    DataSourceContractError,
    DataSourceTransientError,
)
from daily_insights_api.modules.data_sources.tradingview.heatmap import (
    CapturedHeatmap,
    HeatmapImage,
    assemble_heatmap,
)
from daily_insights_api.modules.markets.api import store_index_daily_bars
from daily_insights_api.modules.markets.models import SectorHeatmapImage
from daily_insights_api.modules.orchestration.facts import store_market_bars
from daily_insights_api.modules.orchestration.models import FunctionAttempt, FunctionRun, JobRun
from daily_insights_api.modules.orchestration.worker import ClaimedFunction

pytestmark = pytest.mark.integration

# The routine runs Monday 2026-10-12 at 08:00 Taipei, before Hong Kong opens,
# so the capture shows Friday's close and is dated by that settled bar.
EDITION = date(2026, 10, 12)
SETTLED = date(2026, 10, 9)
CAPTURED_AT = datetime(2026, 10, 12, 0, 5, tzinfo=UTC)


def _png(width: int, height: int, seed: bytes = b"") -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
        + seed
    )


def _captured(seed: bytes = b"", captured_at: datetime = CAPTURED_AT) -> CapturedHeatmap:
    return assemble_heatmap("HSI", captured_at, _png(1920, 989, seed), _png(400, 34, seed))


def _captured_after(settled: date, seed: bytes = b"") -> CapturedHeatmap:
    """A capture at 08:05 HKT the morning after `settled`, so it is dated to it."""
    morning = datetime.combine(settled + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return _captured(seed, morning + timedelta(minutes=5))


def _capture_in_order(monkeypatch: pytest.MonkeyPatch, captures: list[CapturedHeatmap]) -> None:
    """Hand the worker one prepared capture per run, in call order.

    The clock the pre-capture gates read follows the capture about to be
    taken, as it would in production where both come from the same run.
    """
    queue = list(captures)

    async def capture(**_: object) -> CapturedHeatmap:
        return queue.pop(0)

    monkeypatch.setattr(orchestration_functions, "_utcnow", lambda: queue[0].captured_at)
    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.digests: dict[str, str] = {}
        self.deleted: list[str] = []

    async def head(self, ref: ObjectRef) -> ObjectMetadata | None:
        if ref.key not in self.objects:
            return None
        return ObjectMetadata(
            ref=ref,
            size_bytes=len(self.objects[ref.key]),
            mime_type="image/png",
            sha256=self.digests[ref.key],
        )

    async def copy_if_absent(self, source: ObjectRef, target: ObjectRef, *, sha256: str) -> bool:
        raise NotImplementedError

    async def put_if_absent(
        self, target: ObjectRef, content: BinaryIO, *, size_bytes: int, mime_type: str, sha256: str
    ) -> bool:
        raise NotImplementedError

    async def overwrite(
        self, target: ObjectRef, content: BinaryIO, *, size_bytes: int, mime_type: str, sha256: str
    ) -> None:
        assert target.bucket == "daily-insights-test"
        assert mime_type == "image/png"
        body = content.read()
        assert len(body) == size_bytes
        self.objects[target.key] = body
        self.digests[target.key] = sha256

    async def delete(self, target: ObjectRef) -> None:
        self.objects.pop(target.key, None)
        self.digests.pop(target.key, None)
        self.deleted.append(target.key)

    def read(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        raise NotImplementedError

    async def presign_get(self, ref: ObjectRef, expires_in: timedelta) -> str:
        raise NotImplementedError

    async def presign_put(
        self, ref: ObjectRef, *, mime_type: str, sha256: str, expires_in: timedelta
    ) -> str:
        raise NotImplementedError


DEFAULT_SETTINGS: dict[str, Any] = {
    "environment": "test",
    "tradingview_enabled": True,
    "r2_endpoint_url": "https://account.r2.cloudflarestorage.com",
    "r2_bucket_name": "daily-insights-test",
    "r2_access_key_id": SecretStr("key"),
    "r2_secret_access_key": SecretStr("secret"),
    "tradingview_heatmap_retention_days": 30,
}


def _settings(**overrides: Any) -> Settings:
    return Settings.model_validate({**DEFAULT_SETTINGS, **overrides})


def _claimed(
    *,
    function_run_id: uuid.UUID | None = None,
    job_run_id: uuid.UUID | None = None,
    attempt_id: uuid.UUID | None = None,
    fence_token: uuid.UUID | None = None,
    edition_date: date = EDITION,
) -> ClaimedFunction:
    return ClaimedFunction(
        connection=cast(AsyncConnection, None),
        function_run_id=function_run_id or uuid.uuid4(),
        job_run_id=job_run_id or uuid.uuid4(),
        attempt_id=attempt_id or uuid.uuid4(),
        function_key="hsi_sector_heatmap",
        provider_key="tradingview",
        edition_date=edition_date,
        fence_token=fence_token or uuid.uuid4(),
        deadline_at=None,
        scope={},
    )


async def test_disabled_flag_and_missing_r2_are_unavailable_without_capturing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capture(**_: object) -> CapturedHeatmap:
        raise AssertionError("must not capture")

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)
    sessions = cast(async_sessionmaker[AsyncSession], None)

    disabled = await orchestration_functions._run_tradingview_heatmap(
        _settings(tradingview_enabled=False), sessions, _claimed(), FakeObjectStore()
    )
    assert (disabled.status, disabled.error_code) == ("unavailable", "tradingview_disabled")

    no_store = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, _claimed(), None
    )
    assert (no_store.status, no_store.error_code) == ("unavailable", "r2_not_configured")


@pytest.mark.parametrize(
    "error, code, retryable",
    [
        (DataSourceTransientError("timeout at Finance"), "heatmap_capture_failed", True),
        (DataSourceContractError("canvas not found"), "heatmap_contract_drift", False),
    ],
)
async def test_capture_failures_map_to_retryable_and_terminal_outcomes(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    code: str,
    retryable: bool,
) -> None:
    _, sessions = heatmap_database

    async def capture(**_: object) -> CapturedHeatmap:
        raise error

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)

    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions), FakeObjectStore()
    )

    assert outcome.status == "unavailable"
    assert outcome.error_code == code
    assert outcome.retryable is retryable
    assert outcome.error_detail is not None and str(error) in outcome.error_detail


@pytest.fixture(autouse=True)
def _clock_at_default_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker reads the clock before capturing; pin it to the default capture
    time so the pre-capture gate agrees with the stubbed captures."""
    monkeypatch.setattr(orchestration_functions, "_utcnow", lambda: CAPTURED_AT)


@pytest_asyncio.fixture
async def heatmap_database() -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    remigrate_database(database_url)
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, sessions
    finally:
        await engine.dispose()


async def _running_attempt(
    sessions: async_sessionmaker[AsyncSession],
    edition_date: date = EDITION,
    settled: date | None = SETTLED,
    trigger: str = "manual",
) -> ClaimedFunction:
    """A job -> function run -> attempt chain whose lease matches the fence.

    `settled` is the newest ^HSI bar on record (None stores no bar), written to
    both tables the worker fills, as the index cards beside the heatmap read it.
    """
    fence = uuid.uuid4()
    async with sessions.begin() as database:
        job = JobRun(
            job_key="hk_equity_refresh",
            kind="function",
            trigger=trigger,
            registry_version="test",
            registry_snapshot={},
            edition_date=edition_date,
            status="running",
        )
        database.add(job)
        await database.flush()
        function = FunctionRun(
            job_run_id=job.id,
            function_key="hsi_sector_heatmap",
            provider_key="tradingview",
            scope={},
            status="running",
            attempt_count=1,
            lease_token=fence,
        )
        database.add(function)
        await database.flush()
        attempt = FunctionAttempt(
            function_run_id=function.id,
            attempt_number=1,
            provider_key="tradingview",
            function_key="hsi_sector_heatmap",
            scope={},
            fence_token=fence,
            request_metadata=[],
        )
        database.add(attempt)
        await database.flush()
        if settled is not None:
            bar = DailyBar(
                instrument_source_id="^HSI",
                market="hk_equity",
                symbol="^HSI",
                trade_date=settled,
                close=Decimal("26000"),
            )
            await store_index_daily_bars(
                database,
                bars=[bar],
                provider="yfinance",
                contract_version="test",
                source_fetched_at=CAPTURED_AT,
            )
            await store_market_bars(
                database,
                function_run_id=function.id,
                fence_token=fence,
                function_attempt_id=attempt.id,
                provider_key="yahoo_finance",
                dataset_key="hk_index_daily_bars",
                symbol="^HSI",
                market="hk_equity",
                unit="index",
                contract_version="test",
                bars=(bar,),
            )
        return _claimed(
            function_run_id=function.id,
            job_run_id=job.id,
            attempt_id=attempt.id,
            fence_token=fence,
            edition_date=edition_date,
        )


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[SectorHeatmapImage]:
    async with sessions() as database:
        return list(
            await database.scalars(
                select(SectorHeatmapImage).order_by(
                    SectorHeatmapImage.edition_date, SectorHeatmapImage.sort_order
                )
            )
        )


async def test_capture_is_uploaded_recorded_and_idempotent(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = heatmap_database
    store = FakeObjectStore()
    captured = _captured()

    async def capture(**_: object) -> CapturedHeatmap:
        return captured

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)
    claimed = await _running_attempt(sessions)

    first = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, claimed, store
    )

    assert first.status == "succeeded"
    assert first.record_count == 2
    assert first.source_as_of == SETTLED
    assert first.fetched_at == CAPTURED_AT
    assert first.result == {"images": 2, "uploaded": 2, "removed_objects": 0}
    assert sorted(store.objects) == [
        "heatmaps/hsi/2026-10-09/legend.png",
        "heatmaps/hsi/2026-10-09/overview.png",
    ]
    rows = await _rows(sessions)
    assert [(row.kind, row.slug, row.sort_order) for row in rows] == [
        ("overview", "overview", 0),
        ("legend", "legend", 1),
    ]
    overview = rows[0]
    assert overview.name == "Overview"
    assert overview.market_code == "hk_equity"
    assert overview.function_attempt_id == claimed.attempt_id
    assert (overview.width, overview.height, overview.size_bytes) == (
        1920,
        989,
        len(captured.images[0].png),
    )

    # The same capture again changes nothing and uploads nothing.
    second = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions), store
    )
    assert second.status == "no_change"
    assert second.result == {"images": 2, "uploaded": 0, "removed_objects": 0}


async def test_unchanged_capture_still_uploads_when_the_bucket_disagrees(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale attempt's rolled-back upload must not survive behind a matching ledger."""
    _, sessions = heatmap_database
    store = FakeObjectStore()
    captured = _captured()

    async def capture(**_: object) -> CapturedHeatmap:
        return captured

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)
    await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions), store
    )
    # A stale attempt left different bytes under the overview key, then rolled back.
    store.objects["heatmaps/hsi/2026-10-09/overview.png"] = b"stale"
    store.digests["heatmaps/hsi/2026-10-09/overview.png"] = "f" * 64

    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions), store
    )

    assert outcome.status == "no_change"
    assert outcome.result == {"images": 2, "uploaded": 1, "removed_objects": 0}
    assert store.objects["heatmaps/hsi/2026-10-09/overview.png"] == captured.images[0].png


async def test_rerun_replaces_changed_images_and_drops_vanished_slugs(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = heatmap_database
    store = FakeObjectStore()
    # A slug the new capture no longer produces (here: a legacy half) is dropped.
    first = _captured()
    first = CapturedHeatmap(
        first.data_source,
        first.captured_at,
        (
            *first.images,
            HeatmapImage(
                kind="overview",
                slug="overview_left",
                name="Overview (left)",
                sort_order=2,
                png=_png(900, 989),
                width=900,
                height=989,
                sha256="c" * 64,
            ),
        ),
    )
    captures = iter([first, _captured(b"v2")])

    async def capture(**_: object) -> CapturedHeatmap:
        return next(captures)

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)
    await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions), store
    )

    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions), store
    )

    assert outcome.status == "succeeded"
    assert outcome.result == {"images": 2, "uploaded": 2, "removed_objects": 1}
    assert store.deleted == ["heatmaps/hsi/2026-10-09/overview_left.png"]
    assert sorted(store.objects) == [
        "heatmaps/hsi/2026-10-09/legend.png",
        "heatmaps/hsi/2026-10-09/overview.png",
    ]
    rows = await _rows(sessions)
    assert [(row.slug, row.sort_order) for row in rows] == [("overview", 0), ("legend", 1)]


async def test_editions_past_retention_are_removed_from_ledger_and_bucket(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = heatmap_database
    store = FakeObjectStore()
    old_edition = SETTLED - timedelta(days=31)
    kept_edition = SETTLED - timedelta(days=30)
    # Each seed capture is taken the morning after its own settled bar.
    _capture_in_order(
        monkeypatch, [_captured_after(old_edition), _captured_after(kept_edition), _captured()]
    )
    await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions, settled=old_edition), store
    )
    await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions, settled=kept_edition), store
    )

    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions), store
    )

    assert outcome.result == {"images": 2, "uploaded": 2, "removed_objects": 2}
    assert sorted(store.deleted) == [
        f"heatmaps/hsi/{old_edition.isoformat()}/legend.png",
        f"heatmaps/hsi/{old_edition.isoformat()}/overview.png",
    ]
    assert {row.edition_date for row in await _rows(sessions)} == {kept_edition, SETTLED}


@pytest.mark.parametrize(
    ("captured_at", "trading", "last_session"),
    [
        # Monday 08:05 HKT: before the open, Friday is the last completed session.
        (datetime(2026, 10, 12, 0, 5, tzinfo=UTC), False, date(2026, 10, 9)),
        # Monday 09:00 and 15:59 HKT: trading (the lunch break still counts).
        (datetime(2026, 10, 12, 1, 0, tzinfo=UTC), True, date(2026, 10, 9)),
        (datetime(2026, 10, 12, 7, 59, tzinfo=UTC), True, date(2026, 10, 9)),
        # Monday 16:29 HKT: the closing auction's result is still in the
        # 15-minute delay.
        (datetime(2026, 10, 12, 8, 29, tzinfo=UTC), True, date(2026, 10, 9)),
        # Monday 16:30 HKT: today is complete.
        (datetime(2026, 10, 12, 8, 30, tzinfo=UTC), False, date(2026, 10, 12)),
        # Saturday noon HKT: Friday.
        (datetime(2026, 10, 17, 4, 0, tzinfo=UTC), False, date(2026, 10, 16)),
        # National Day (Thu 1 Oct) is a holiday: Friday morning dates to Wednesday.
        (datetime(2026, 10, 2, 0, 5, tzinfo=UTC), False, date(2026, 9, 30)),
        # Chung Yeung falls on Monday 19 Oct: 11:00 that day is not trading, and
        # Tuesday morning dates to the previous Friday.
        (datetime(2026, 10, 19, 3, 0, tzinfo=UTC), False, date(2026, 10, 16)),
        (datetime(2026, 10, 20, 0, 5, tzinfo=UTC), False, date(2026, 10, 16)),
        # Christmas Eve is a half day closing at 12:00: 11:00 trades, 12:30 is settled.
        (datetime(2026, 12, 24, 3, 0, tzinfo=UTC), True, date(2026, 12, 23)),
        (datetime(2026, 12, 24, 4, 30, tzinfo=UTC), False, date(2026, 12, 24)),
    ],
)
def test_hk_heatmap_session_follows_the_hong_kong_clock(
    captured_at: datetime, trading: bool, last_session: date
) -> None:
    assert orchestration_functions.hk_heatmap_session(captured_at) == (trading, last_session)


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        # An operator's refresh during the session is a readable skip, not a
        # failure that burns the retry budget in fifteen minutes.
        ("manual", ("no_change", None, False)),
        # The scheduled job should never be here (it runs before the open); if
        # it is, say so plainly and do not retry into the same session.
        ("automatic", ("unavailable", "hk_market_open", False)),
    ],
)
async def test_capture_during_the_session_is_not_stored(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
    expected: tuple[str, str | None, bool],
) -> None:
    """An intraday capture must not overwrite the last settled day's snapshot."""
    _, sessions = heatmap_database
    store = FakeObjectStore()
    captures = iter(
        [
            _captured(),
            # Monday 11:00 HKT, a manual trigger while Hong Kong trades.
            _captured(b"intraday", datetime(2026, 10, 12, 3, 0, tzinfo=UTC)),
        ]
    )

    async def capture(**_: object) -> CapturedHeatmap:
        return next(captures)

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)
    await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions), store
    )
    settled = {row.slug: row.sha256 for row in await _rows(sessions)}
    settled_objects = dict(store.objects)

    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions, trigger=trigger), store
    )

    assert (outcome.status, outcome.error_code, outcome.retryable) == expected
    if trigger == "manual":
        assert outcome.result == {"skipped": "hk_market_open"}
    assert {row.slug: row.sha256 for row in await _rows(sessions)} == settled
    assert store.objects == settled_objects


async def test_a_sweep_that_only_removes_objects_is_not_no_change(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = heatmap_database
    store = FakeObjectStore()
    old_edition = SETTLED - timedelta(days=31)
    _capture_in_order(monkeypatch, [_captured_after(old_edition), _captured(), _captured()])
    keep_longer = _settings(tradingview_heatmap_retention_days=60)
    await orchestration_functions._run_tradingview_heatmap(
        keep_longer, sessions, await _running_attempt(sessions, settled=old_edition), store
    )
    await orchestration_functions._run_tradingview_heatmap(
        keep_longer, sessions, await _running_attempt(sessions), store
    )

    # Same capture, shorter retention: no row changes, two objects removed.
    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions), store
    )

    assert outcome.status == "succeeded"
    assert outcome.record_count == 0
    assert outcome.result == {"images": 2, "uploaded": 0, "removed_objects": 2}
    assert {row.edition_date for row in await _rows(sessions)} == {SETTLED}


async def test_failed_object_delete_keeps_the_rows_for_the_next_run(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = heatmap_database

    class FlakyDeleteStore(FakeObjectStore):
        fail_deletes = False

        async def delete(self, target: ObjectRef) -> None:
            if self.fail_deletes:
                raise RuntimeError("r2 unreachable")
            await super().delete(target)

    store = FlakyDeleteStore()
    old_edition = SETTLED - timedelta(days=31)
    _capture_in_order(monkeypatch, [_captured_after(old_edition), _captured(), _captured()])
    await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions, settled=old_edition), store
    )

    # The retention sweep's R2 delete fails: the expired rows must survive so a
    # later run still knows which objects to delete.
    store.fail_deletes = True
    with pytest.raises(RuntimeError, match="r2 unreachable"):
        await orchestration_functions._run_tradingview_heatmap(
            _settings(), sessions, await _running_attempt(sessions), store
        )
    assert {row.edition_date for row in await _rows(sessions)} == {old_edition}
    # The expired objects are still there for the retry; the new edition's
    # uploads landed before the failure and are simply re-verified next run.
    assert {
        f"heatmaps/hsi/{old_edition.isoformat()}/legend.png",
        f"heatmaps/hsi/{old_edition.isoformat()}/overview.png",
    } <= set(store.objects)

    store.fail_deletes = False
    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions), store
    )

    assert outcome.result == {"images": 2, "uploaded": 2, "removed_objects": 2}
    assert {row.edition_date for row in await _rows(sessions)} == {SETTLED}
    assert sorted(store.objects) == [
        "heatmaps/hsi/2026-10-09/legend.png",
        "heatmaps/hsi/2026-10-09/overview.png",
    ]


@pytest.mark.parametrize(
    ("now", "trigger", "expected"),
    [
        # Monday 11:00 HKT: the session is open.
        (datetime(2026, 10, 12, 3, 0, tzinfo=UTC), "manual", ("no_change", None)),
        (datetime(2026, 10, 12, 3, 0, tzinfo=UTC), "automatic", ("unavailable", "hk_market_open")),
    ],
)
async def test_gate_runs_before_the_browser_session(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    trigger: str,
    expected: tuple[str, str | None],
) -> None:
    _, sessions = heatmap_database
    monkeypatch.setattr(orchestration_functions, "_utcnow", lambda: now)

    async def capture(**_: object) -> CapturedHeatmap:
        raise AssertionError("must not pay for a browser session the gate would discard")

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)

    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions, trigger=trigger), FakeObjectStore()
    )

    assert (outcome.status, outcome.error_code) == expected


async def test_lease_lost_during_upload_rolls_the_ledger_back(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fence check that fails after writes must leave the transaction by rollback."""
    _, sessions = heatmap_database
    claimed = await _running_attempt(sessions)

    class LeaseStealingStore(FakeObjectStore):
        async def overwrite(self, target: ObjectRef, content: BinaryIO, **kwargs: Any) -> None:
            await super().overwrite(target, content, **kwargs)
            # Another worker takes the lease over while this attempt is mid-upload.
            async with sessions.begin() as database:
                await database.execute(
                    update(FunctionRun)
                    .where(FunctionRun.id == claimed.function_run_id)
                    .values(lease_token=uuid.uuid4())
                )

    store = LeaseStealingStore()

    async def capture(**_: object) -> CapturedHeatmap:
        return _captured()

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)

    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, claimed, store
    )

    assert outcome.status == "cancelled"
    # The rows this attempt upserted were rolled back, not committed.
    assert await _rows(sessions) == []


async def test_stale_fence_cancels_without_touching_the_ledger(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = heatmap_database
    store = FakeObjectStore()

    async def capture(**_: object) -> CapturedHeatmap:
        return _captured()

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)
    claimed = await _running_attempt(sessions)
    claimed.fence_token = uuid.uuid4()
    # A lost lease is noticed before the browser session, not after it.
    monkeypatch.setattr(
        orchestration_functions,
        "capture_sector_heatmap",
        lambda **_: (_ for _ in ()).throw(AssertionError("must not capture")),
    )

    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, claimed, store
    )

    assert outcome.status == "cancelled"
    assert await _rows(sessions) == []
    # The stale attempt must not have uploaded anything either.
    assert store.objects == {}


def test_object_keys_are_namespaced_by_source_and_edition() -> None:
    assert (
        orchestration_functions.heatmap_object_key("HSI", EDITION, "finance")
        == "heatmaps/hsi/2026-10-12/finance.png"
    )
