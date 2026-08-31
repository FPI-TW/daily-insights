import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.models import Base
from daily_insights_api.modules.data_sources.api import (
    DailyBar,
    DataSourceContractError,
    Provenance,
)
from daily_insights_api.modules.data_sources.twelve_data.adapter import (
    DailyBarsResult,
    QuoteResult,
    TwelveDataAdapter,
)
from daily_insights_api.modules.markets.catalog import MARKETS
from daily_insights_api.modules.markets.models import Market
from daily_insights_api.modules.operations.models import SourceRun
from daily_insights_api.modules.reports.models import PublicationSourceRun, ReportPublication
from daily_insights_api.modules.reports.morning_report import (
    MORNING_REPORT_DERIVATION_VERSION,
    _run_market,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def macro_report_database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required for integration tests")
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
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
    try:
        yield session_factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class DeterministicMacroAdapter:
    quote_mode: str = "success"
    history_mode: str = "success"
    quote_marker: str = "quote-v1"
    history_marker: str = "history-v1"

    async def get_quote(
        self,
        *,
        market: str,
        symbol: str,
        expected_currency: str,
    ) -> QuoteResult:
        assert market == "global_macro_bonds"
        assert expected_currency == ("EUR" if symbol == "HG1" else "USD")
        if self.quote_mode == "failed":
            raise DataSourceContractError("api_key=quote-secret")
        as_of = date(2026, 8, 30)
        return QuoteResult(
            symbol=symbol,
            name=None,
            currency=expected_currency,
            as_of=as_of,
            close=Decimal("1"),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            volume=None,
            change=Decimal("0"),
            percent_change=Decimal("0"),
            provenance=_provenance(
                endpoint="/quote",
                marker=f"{self.quote_marker}:{symbol}",
                as_of=as_of,
                record_count=1,
            ),
        )

    async def get_daily_bars(
        self,
        *,
        market: str,
        symbol: str,
        expected_currency: str,
        outputsize: int,
        expected_asset_type: str | None = None,
    ) -> DailyBarsResult:
        assert market == "global_macro_bonds"
        assert expected_currency == "USD"
        assert outputsize == 500
        assert expected_asset_type in {"Energy Resource", "Precious Metal"}
        if self.history_mode == "failed":
            raise DataSourceContractError("token=history-secret")
        start = date(2026, 7, 31)
        items = tuple(
            DailyBar(
                instrument_source_id=symbol,
                market="global_macro_bonds",
                symbol=symbol,
                trade_date=start + timedelta(days=index),
                open=Decimal(index + 1),
                high=Decimal(index + 2),
                low=Decimal(index + 1),
                close=Decimal(index + 1),
                source="twelve_data",
            )
            for index in range(30)
        )
        return DailyBarsResult(
            items=items,
            provenance=_provenance(
                endpoint="/time_series",
                marker=f"{self.history_marker}:{symbol}",
                as_of=items[-1].trade_date,
                record_count=len(items),
            ),
        )


def _provenance(*, endpoint: str, marker: str, as_of: date, record_count: int) -> Provenance:
    return Provenance(
        provider="twelve_data",
        contract_version="test.v1",
        contract_hash=_digest("contract"),
        endpoint=endpoint,
        query_fingerprint=_digest(f"query:{marker}"),
        fetched_at=datetime(2026, 8, 30, tzinfo=UTC),
        as_of=as_of,
        response_digest=_digest(marker),
        record_count=record_count,
    )


async def _publication_state(
    session_factory: async_sessionmaker[AsyncSession], revision: int
) -> tuple[ReportPublication, list[SourceRun], list[PublicationSourceRun]]:
    async with session_factory() as database:
        publication = (
            await database.scalars(
                select(ReportPublication)
                .where(
                    ReportPublication.market_code == "global_macro_bonds",
                    ReportPublication.revision == revision,
                )
                .order_by(ReportPublication.revision)
            )
        ).one()
        sources = list(
            (
                await database.scalars(
                    select(SourceRun)
                    .where(SourceRun.pipeline_run_id == publication.pipeline_run_id)
                    .order_by(SourceRun.dataset_key)
                )
            ).all()
        )
        links = list(
            (
                await database.scalars(
                    select(PublicationSourceRun).where(
                        PublicationSourceRun.publication_id == publication.id
                    )
                )
            ).all()
        )
    return publication, sources, links


def _expected_input_digest(sources: list[SourceRun]) -> str:
    material = "|".join(
        sorted(
            f"{source.provider}:{source.dataset_key}:{source.status}:"
            f"{source.payload_sha256 or source.error_code or 'unknown'}"
            for source in sources
        )
    )
    return _digest(f"{MORNING_REPORT_DERIVATION_VERSION}|{material}")


async def _counts(session_factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with session_factory() as database:
        publications = (await database.scalars(select(ReportPublication))).all()
        sources = (await database.scalars(select(SourceRun))).all()
    return len(publications), len(sources)


def _blocks(publication: ReportPublication) -> list[dict[str, object]]:
    blocks = publication.content["blocks"]
    assert isinstance(blocks, list)
    assert all(isinstance(block, dict) for block in blocks)
    return blocks


async def test_macro_orchestration_persists_all_dataset_outcomes_and_revisions(
    macro_report_database: async_sessionmaker[AsyncSession],
) -> None:
    adapter = DeterministicMacroAdapter()
    adapter_for_run = cast(TwelveDataAdapter, adapter)
    edition_date = date(2026, 8, 31)

    await _run_market(macro_report_database, adapter_for_run, "global_macro_bonds", edition_date)
    complete, complete_sources, complete_links = await _publication_state(macro_report_database, 1)

    assert complete.source_as_of == date(2026, 8, 29)
    assert complete.content["status"] == "complete"
    assert complete.input_digest == _expected_input_digest(complete_sources)
    assert [block["id"] for block in _blocks(complete)] == [
        "macro.commodities",
        "macro.commodity_normalized_performance",
    ]
    assert [block["status"] for block in _blocks(complete)] == ["ok", "ok"]
    assert [source.dataset_key for source in complete_sources] == [
        "macro.commodity_daily_bars",
        "macro.commodity_quotes",
    ]
    assert all(source.status == "succeeded" for source in complete_sources)
    assert {
        source.dataset_key: (source.provider, source.endpoint) for source in complete_sources
    } == {
        "macro.commodity_quotes": ("twelve_data", "/quote"),
        "macro.commodity_daily_bars": ("twelve_data", "/time_series"),
    }
    assert all(source.payload_sha256 is not None for source in complete_sources)
    assert all(
        source.record_count is not None and source.record_count > 0 for source in complete_sources
    )
    assert all(
        source.source_as_of is not None
        and source.fetched_at is not None
        and source.finished_at is not None
        for source in complete_sources
    )
    assert all(
        source.error_code is None and source.error_detail is None for source in complete_sources
    )
    assert {link.source_run_id for link in complete_links} == {
        source.id for source in complete_sources
    }
    assert len(complete_links) == 2
    assert not {"payload", "raw_payload", "raw_rows", "rows"} & set(
        SourceRun.__table__.columns.keys()
    )

    await _run_market(macro_report_database, adapter_for_run, "global_macro_bonds", edition_date)
    assert await _counts(macro_report_database) == (1, 2)

    adapter.history_mode = "failed"
    await _run_market(macro_report_database, adapter_for_run, "global_macro_bonds", edition_date)
    partial, partial_sources, partial_links = await _publication_state(macro_report_database, 2)

    assert partial.source_as_of == date(2026, 8, 30)
    assert partial.content["status"] == "partial"
    assert partial.input_digest == _expected_input_digest(partial_sources)
    assert [block["status"] for block in _blocks(partial)] == ["ok", "error"]
    assert {source.dataset_key: source.status for source in partial_sources} == {
        "macro.commodity_quotes": "succeeded",
        "macro.commodity_daily_bars": "failed",
    }
    failed_history = next(
        source for source in partial_sources if source.dataset_key == "macro.commodity_daily_bars"
    )
    assert failed_history.payload_sha256 is None
    assert failed_history.record_count is None
    assert failed_history.source_as_of is None
    assert failed_history.fetched_at is None
    assert failed_history.finished_at is not None
    assert failed_history.error_code == "datasourcecontracterror"
    assert failed_history.error_detail is not None
    assert "history-secret" not in failed_history.error_detail
    assert "[REDACTED]" in failed_history.error_detail
    assert {link.source_run_id for link in partial_links} == {
        source.id for source in partial_sources
    }
    assert len(partial_links) == 2

    await _run_market(macro_report_database, adapter_for_run, "global_macro_bonds", edition_date)
    assert await _counts(macro_report_database) == (2, 4)

    adapter.quote_mode = "failed"
    await _run_market(macro_report_database, adapter_for_run, "global_macro_bonds", edition_date)
    unavailable, unavailable_sources, unavailable_links = await _publication_state(
        macro_report_database, 3
    )

    assert unavailable.source_as_of is None
    assert unavailable.content["status"] == "unavailable"
    assert unavailable.input_digest == _expected_input_digest(unavailable_sources)
    assert [block["status"] for block in _blocks(unavailable)] == ["error", "error"]
    assert all(source.status == "failed" for source in unavailable_sources)
    assert all(
        source.source_as_of is None
        and source.fetched_at is None
        and source.finished_at is not None
        and source.payload_sha256 is None
        and source.record_count is None
        for source in unavailable_sources
    )
    assert len(unavailable_links) == 2
    assert {link.source_run_id for link in unavailable_links} == {
        source.id for source in unavailable_sources
    }

    adapter.quote_mode = "success"
    adapter.history_mode = "success"
    adapter.quote_marker = "quote-v2"
    await _run_market(macro_report_database, adapter_for_run, "global_macro_bonds", edition_date)
    corrected, corrected_sources, _ = await _publication_state(macro_report_database, 4)

    assert corrected.input_digest == _expected_input_digest(corrected_sources)
    assert corrected.input_digest != complete.input_digest
    assert [block["status"] for block in _blocks(corrected)] == ["ok", "ok"]
    assert await _counts(macro_report_database) == (4, 8)
