from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from daily_insights_api.modules.data_sources.api import (
    DataSourceContractError,
    Provenance,
    QuoteResult,
    QuotesResult,
)
from daily_insights_api.modules.reports.contracts import TableBlock
from daily_insights_api.modules.reports.morning_report import (
    _build_dataset_blocks,
    _market_datasets,
    _previous_close_change,
    _validate_manifest_output,
)


def _quote(symbol: str, close: str, previous_close: str) -> QuoteResult:
    return QuoteResult(
        symbol=symbol,
        name=None,
        currency="USD",
        as_of=date(2026, 9, 2),
        close=Decimal(close),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        volume=None,
        previous_close=Decimal(previous_close),
        change=None,
        percent_change=Decimal("999"),
        provenance=Provenance(
            provider="twelve_data",
            contract_version="test",
            contract_hash="a" * 64,
            endpoint="/quote",
            query_fingerprint="b" * 64,
            fetched_at=datetime(2026, 9, 3, tzinfo=UTC),
            as_of=date(2026, 9, 2),
            response_digest="c" * 64,
            record_count=1,
        ),
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

    async def get_quotes(
        self,
        *,
        market: str,
        symbols: tuple[str, ...],
        expected_currencies: dict[str, str],
        symbol_types: dict[str, str] | None = None,
    ) -> QuotesResult:
        assert market == "us_equity"
        assert symbol_types == {}
        self.calls.append((symbols, expected_currencies))
        items = tuple(_quote(symbol, *PRICES[symbol]) for symbol in symbols)
        return QuotesResult(items=items, provenances=(items[0].provenance,))


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
