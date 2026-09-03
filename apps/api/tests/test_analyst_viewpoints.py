import os
from datetime import UTC, date, datetime
from pathlib import Path as FileSystemPath
from typing import Any, cast

import pytest
from anyio import Path
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.models import Base
from daily_insights_api.modules.analyst_viewpoints.models import AnalystViewpoint
from daily_insights_api.modules.analyst_viewpoints.schemas import (
    AnalystViewpointSyncResponse,
    SyncMarketStatus,
    UpstreamSummary,
)
from daily_insights_api.modules.analyst_viewpoints.service import (
    MARKET_MAPPING,
    AnalystViewpointClient,
    AnalystViewpointSyncError,
    sync_viewpoints,
)
from daily_insights_api.modules.markets.catalog import MARKETS
from daily_insights_api.modules.markets.models import Market
from daily_insights_api.scripts import run_analyst_viewpoints


def _upstream_summary(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "us_macro": [],
        "forex": [],
        "crypto": [],
        "us_stocks": [],
        "hk_stocks": [],
        "cn_stocks": [],
        "tw_stocks": [],
        "tw_futures": [],
    }
    return {**values, **overrides}


def test_upstream_summary_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        UpstreamSummary.model_validate(_upstream_summary(extra=[]))


def test_upstream_summary_rejects_malformed_market_and_normalizes_blanks() -> None:
    with pytest.raises(ValidationError):
        UpstreamSummary.model_validate(_upstream_summary(us_macro=["valid"], crypto={}))
    summary = UpstreamSummary.model_validate(
        _upstream_summary(us_macro=["  Rates remain restrictive  ", "", "  "])
    )
    assert summary.us_macro == ["Rates remain restrictive"]


def test_upstream_summary_accepts_the_documented_eight_market_response() -> None:
    summary = UpstreamSummary.model_validate(
        _upstream_summary(
            us_macro=["Macro point"],
            forex=["Forex point"],
            crypto=["Crypto point"],
            us_stocks=["US stock point"],
            hk_stocks=["Hong Kong point"],
            cn_stocks=["China point"],
            tw_stocks=["Taiwan point"],
            tw_futures=["Futures point"],
        )
    )
    assert summary.tw_futures == ["Futures point"]


def test_market_mapping_persists_all_eight_upstream_markets() -> None:
    assert MARKET_MAPPING == {
        "us_macro": "global_macro_bonds",
        "forex": "forex",
        "crypto": "crypto",
        "us_stocks": "us_equity",
        "hk_stocks": "hk_equity",
        "cn_stocks": "cn_equity",
        "tw_stocks": "tw_equity",
        "tw_futures": "tw_index_derivatives",
    }


class _Client:
    def __init__(
        self,
        summary: UpstreamSummary,
        *,
        fetched_at: datetime = datetime(2026, 9, 2, 1, tzinfo=UTC),
    ) -> None:
        self.summary = summary
        self.fetched_at = fetched_at

    async def fetch_summary(self) -> tuple[UpstreamSummary, datetime]:
        return self.summary, self.fetched_at


class _Transaction:
    def __init__(self) -> None:
        self.database = object()
        self.exit_error: type[BaseException] | None = None

    async def __aenter__(self) -> object:
        return self.database

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: object,
    ) -> None:
        del error, traceback
        self.exit_error = error_type


class _SessionFactory:
    def __init__(self) -> None:
        self.transactions: list[_Transaction] = []

    def begin(self) -> _Transaction:
        transaction = _Transaction()
        self.transactions.append(transaction)
        return transaction


class _StaleUpsertResult:
    def scalar_one_or_none(self) -> None:
        return None


class _StaleUpsertDatabase:
    def __init__(self) -> None:
        self.statement_sql = ""

    async def execute(self, statement: Any) -> _StaleUpsertResult:
        dialect = cast(Any, postgresql.dialect)()
        self.statement_sql = str(statement.compile(dialect=dialect))
        return _StaleUpsertResult()


async def test_sync_does_not_overwrite_a_newer_viewpoint_with_stale_fetch() -> None:
    database = _StaleUpsertDatabase()
    result = await sync_viewpoints(
        cast(AsyncSession, database),
        _Client(
            UpstreamSummary(
                us_macro=["Older response"],
                forex=[],
                crypto=[],
                us_stocks=[],
                hk_stocks=[],
                cn_stocks=[],
                tw_stocks=[],
                tw_futures=[],
            )
        ),
        date(2026, 9, 2),
    )

    assert result.markets[0].status == "stale"
    assert "WHERE excluded.fetched_at >= analyst_viewpoints.fetched_at" in database.statement_sql


async def test_scheduler_records_success_and_failure_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: FileSystemPath,
) -> None:
    recorded: list[dict[str, object]] = []
    target_date = date(2026, 9, 2)
    result = AnalystViewpointSyncResponse(
        viewpoint_date=target_date,
        fetched_at=datetime(2026, 9, 2, 1, tzinfo=UTC),
        status="partial",
        markets=[
            SyncMarketStatus(
                source_market_code="us_macro",
                market_code="global_macro_bonds",
                status="updated",
            )
        ],
    )

    async def record(_: object, **kwargs: object) -> None:
        recorded.append(kwargs)

    async def successful_sync(_: object, __: object, ___: date) -> AnalystViewpointSyncResponse:
        return result

    monkeypatch.setattr(run_analyst_viewpoints, "sync_viewpoints", successful_sync)
    monkeypatch.setattr(run_analyst_viewpoints, "record_sync_execution", record)
    runner = run_analyst_viewpoints.build_runner(
        cast(async_sessionmaker[AsyncSession], _SessionFactory()),
        cast(AnalystViewpointClient, object()),
        Path(tmp_path / "heartbeat"),
    )
    assert await runner(target_date) == "partial"
    assert recorded == [{"viewpoint_date": target_date, "trigger": "scheduler", "result": result}]

    async def failed_sync(_: object, __: object, ___: date) -> AnalystViewpointSyncResponse:
        raise AnalystViewpointSyncError("not available", code="upstream_unavailable")

    monkeypatch.setattr(run_analyst_viewpoints, "sync_viewpoints", failed_sync)
    with pytest.raises(AnalystViewpointSyncError):
        await runner(target_date)
    assert recorded[-1] == {
        "viewpoint_date": target_date,
        "trigger": "scheduler",
        "error_code": "upstream_unavailable",
    }


async def test_scheduler_persists_unexpected_failure_in_an_independent_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: FileSystemPath,
) -> None:
    factory = _SessionFactory()
    target_date = date(2026, 9, 2)
    persisted_databases: list[object] = []

    async def unexpected_sync(_: object, __: object, ___: date) -> AnalystViewpointSyncResponse:
        raise RuntimeError("database persistence failed")

    async def record(database: object, **kwargs: object) -> None:
        persisted_databases.append(database)
        assert kwargs == {
            "viewpoint_date": target_date,
            "trigger": "scheduler",
            "error_code": "RuntimeError",
        }

    monkeypatch.setattr(run_analyst_viewpoints, "sync_viewpoints", unexpected_sync)
    monkeypatch.setattr(run_analyst_viewpoints, "record_sync_execution", record)
    runner = run_analyst_viewpoints.build_runner(
        cast(async_sessionmaker[AsyncSession], factory),
        cast(AnalystViewpointClient, object()),
        Path(tmp_path / "heartbeat"),
    )

    with pytest.raises(RuntimeError, match="database persistence failed"):
        await runner(target_date)

    assert len(factory.transactions) == 2
    assert factory.transactions[0].exit_error is RuntimeError
    assert factory.transactions[1].exit_error is None
    assert persisted_databases == [factory.transactions[1].database]
    assert persisted_databases[0] is not factory.transactions[0].database


@pytest.mark.integration
async def test_sync_upserts_present_values_without_erasing_missing_market() -> None:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL") or os.getenv(
        "DAILY_INSIGHTS_DATABASE_URL"
    )
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required for integration tests")
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory.begin() as database:
        database.add_all(
            [
                Market(
                    code=market.code,
                    name_en=market.name_en,
                    name_zh_hant=market.name_zh_hant,
                    name_zh_hans=market.name_zh_hans,
                )
                for market in MARKETS
            ]
        )

    target_date = date(2026, 9, 2)
    async with session_factory.begin() as database:
        first = await sync_viewpoints(
            database,
            _Client(
                UpstreamSummary(
                    us_macro=["First valid point"],
                    forex=[],
                    crypto=["Crypto point"],
                    us_stocks=[],
                    hk_stocks=[],
                    cn_stocks=[],
                    tw_stocks=[],
                    tw_futures=[],
                ),
                fetched_at=datetime(2026, 9, 2, 1, tzinfo=UTC),
            ),
            target_date,
        )
        assert first.status == "partial"

    async with session_factory.begin() as database:
        second = await sync_viewpoints(
            database,
            _Client(
                UpstreamSummary(
                    us_macro=["Replacement point"],
                    forex=[],
                    crypto=[],
                    us_stocks=[],
                    hk_stocks=[],
                    cn_stocks=[],
                    tw_stocks=[],
                    tw_futures=[],
                ),
                fetched_at=datetime(2026, 9, 2, 2, tzinfo=UTC),
            ),
            target_date,
        )
        assert second.status == "partial"

    async with session_factory.begin() as database:
        stale = await sync_viewpoints(
            database,
            _Client(
                UpstreamSummary(
                    us_macro=["Stale replacement point"],
                    forex=[],
                    crypto=[],
                    us_stocks=[],
                    hk_stocks=[],
                    cn_stocks=[],
                    tw_stocks=[],
                    tw_futures=[],
                ),
                fetched_at=datetime(2026, 9, 2, 1, tzinfo=UTC),
            ),
            target_date,
        )
        assert stale.status == "partial"
        assert stale.markets[0].status == "stale"

    async with session_factory() as database:
        crypto = await database.scalar(
            select(AnalystViewpoint).where(
                AnalystViewpoint.viewpoint_date == target_date,
                AnalystViewpoint.market_code == "crypto",
            )
        )
        us_macro = await database.scalar(
            select(AnalystViewpoint).where(
                AnalystViewpoint.viewpoint_date == target_date,
                AnalystViewpoint.market_code == "global_macro_bonds",
            )
        )
    assert crypto is not None and crypto.points == ["Crypto point"]
    assert us_macro is not None and us_macro.points == ["Replacement point"]
    await engine.dispose()
