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
from sqlalchemy import select
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


def _captured(seed: bytes = b"") -> CapturedHeatmap:
    return assemble_heatmap("HSI", CAPTURED_AT, _png(1920, 989, seed), _png(400, 34, seed))


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def head(self, ref: ObjectRef) -> ObjectMetadata | None:
        raise NotImplementedError

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

    async def delete(self, target: ObjectRef) -> None:
        self.objects.pop(target.key, None)
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
    attempt_id: uuid.UUID | None = None,
    fence_token: uuid.UUID | None = None,
    edition_date: date = EDITION,
) -> ClaimedFunction:
    return ClaimedFunction(
        connection=cast(AsyncConnection, None),
        function_run_id=function_run_id or uuid.uuid4(),
        job_run_id=uuid.uuid4(),
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
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str, retryable: bool
) -> None:
    async def capture(**_: object) -> CapturedHeatmap:
        raise error

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)

    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), cast(async_sessionmaker[AsyncSession], None), _claimed(), FakeObjectStore()
    )

    assert outcome.status == "unavailable"
    assert outcome.error_code == code
    assert outcome.retryable is retryable
    assert outcome.error_detail is not None and str(error) in outcome.error_detail


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
) -> ClaimedFunction:
    """A job -> function run -> attempt chain whose lease matches the fence.

    `settled` is the newest ^HSI bar on record (None stores no bar), which is
    what the heatmap edition is derived from.
    """
    fence = uuid.uuid4()
    async with sessions.begin() as database:
        job = JobRun(
            job_key="hk_equity_refresh",
            kind="function",
            trigger="manual",
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
                bars=(
                    DailyBar(
                        instrument_source_id="^HSI",
                        market="hk_equity",
                        symbol="^HSI",
                        trade_date=settled,
                        close=Decimal("26000"),
                    ),
                ),
            )
        return _claimed(
            function_run_id=function.id,
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
    captured = _captured()

    async def capture(**_: object) -> CapturedHeatmap:
        return captured

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)
    old_edition = SETTLED - timedelta(days=31)
    await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions, settled=old_edition), store
    )
    kept_edition = SETTLED - timedelta(days=30)
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


async def test_missing_hk_bars_are_unavailable_and_retryable(
    heatmap_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = heatmap_database
    store = FakeObjectStore()

    async def capture(**_: object) -> CapturedHeatmap:
        return _captured()

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)

    outcome = await orchestration_functions._run_tradingview_heatmap(
        _settings(), sessions, await _running_attempt(sessions, settled=None), store
    )

    assert (outcome.status, outcome.error_code, outcome.retryable) == (
        "unavailable",
        "hk_bars_missing",
        True,
    )
    assert store.objects == {}
    assert await _rows(sessions) == []


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
    captured = _captured()

    async def capture(**_: object) -> CapturedHeatmap:
        return captured

    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)
    old_edition = SETTLED - timedelta(days=31)
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
