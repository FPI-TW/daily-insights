from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from daily_insights_api.modules.data_sources.api import (
    CompletedPriceResult,
    CompletedPricesResult,
    DailyBar,
    DataSourceContractError,
    Provenance,
)
from daily_insights_api.modules.reports.contracts import TableBlock
from daily_insights_api.modules.reports.morning_report import (
    _build_dataset_blocks,
    _market_datasets,
    _previous_close_change,
    _validate_manifest_output,
)


def _price(symbol: str, close: str, previous_close: str) -> CompletedPriceResult:
    provenance = Provenance(
        provider="twelve_data",
        contract_version="test",
        contract_hash="a" * 64,
        endpoint="/time_series",
        query_fingerprint="b" * 64,
        fetched_at=datetime(2026, 9, 3, tzinfo=UTC),
        as_of=date(2026, 9, 2),
        response_digest="c" * 64,
        record_count=2,
    )
    bars = tuple(
        DailyBar(
            instrument_source_id=symbol,
            market="us_equity",
            symbol=symbol,
            trade_date=day,
            open=value,
            high=value,
            low=value,
            close=value,
            volume=None,
            source="twelve_data",
        )
        for day, value in (
            (date(2026, 9, 1), Decimal(previous_close)),
            (date(2026, 9, 2), Decimal(close)),
        )
    )
    return CompletedPriceResult(
        symbol=symbol,
        currency="USD",
        as_of=date(2026, 9, 2),
        close=Decimal(close),
        previous_close=Decimal(previous_close),
        bars=bars,
        provenances=(provenance,),
    )


PRICES = {
    "AAPL": ("324.95999", "325.13"),
    "MSFT": ("100", "100"),
    "NVDA": ("110", "100"),
    "GOOGL": ("337.12", "335.019989"),
    "AMZN": ("90", "100"),
    "META": ("101", "100"),
    "AVGO": ("367.23999", "369.67999"),
    "TSLA": ("120", "100"),
}


@dataclass
class BasketAdapter:
    calls: list[tuple[tuple[str, ...], dict[str, str]]]

    async def get_completed_prices(
        self,
        *,
        market: str,
        symbols: tuple[str, ...],
        expected_currencies: dict[str, str],
        symbol_types: dict[str, str] | None = None,
        outputsize: int = 2,
        expected_asset_types: dict[str, str] | None = None,
    ) -> CompletedPricesResult:
        assert market == "us_equity"
        assert symbol_types == {}
        assert outputsize == 2
        assert expected_asset_types is None
        self.calls.append((symbols, expected_currencies))
        items = tuple(_price(symbol, *PRICES[symbol]) for symbol in symbols)
        return CompletedPricesResult(items=items, provenances=items[0].provenances)


async def test_us_equity_block_comes_from_fixed_mega_cap_basket() -> None:
    adapter = BasketAdapter(calls=[])
    (mega_caps_dataset,) = _market_datasets("us_equity")

    (mega_caps,), _ = await _build_dataset_blocks(adapter, "us_equity", mega_caps_dataset)  # type: ignore[arg-type]

    # The fixed basket is fetched as one batch, never as per-symbol calls.
    assert [symbols for symbols, _ in adapter.calls] == [
        ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA"),
    ]
    assert isinstance(mega_caps, TableBlock)
    # Sorted by our percent change, descending; the provider's figure is ignored.
    assert [row[0].text if row[0] is not None else None for row in mega_caps.rows] == [
        "TSLA",
        "NVDA",
        "META",
        "GOOGL",
        "MSFT",
        "AAPL",
        "AVGO",
        "AMZN",
    ]
    changes = [row[2].value if row[2] is not None else None for row in mega_caps.rows]
    assert changes[0] == Decimal("20.00")
    assert changes[4] == Decimal("0.00")
    assert changes[-1] == Decimal("-10.00")
    assert [column.unit_code for column in mega_caps.columns] == [None, "usd", "percent"]
    _validate_manifest_output("us_equity", (mega_caps,))


def test_previous_close_change_is_our_definition() -> None:
    assert _previous_close_change(Decimal("110"), Decimal("100")) == Decimal("10")
    with pytest.raises(DataSourceContractError, match="previous close"):
        _previous_close_change(Decimal("110"), Decimal("0"))
    with pytest.raises(DataSourceContractError, match="previous close"):
        _previous_close_change(Decimal("110"), None)
