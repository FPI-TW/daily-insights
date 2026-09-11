from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from daily_insights_api.modules.data_sources.api import (
    DataSourceContractError,
    DataSourceTransientError,
    TwseAdapter,
)
from daily_insights_api.modules.data_sources.twse import (
    TaiexDailyBars,
    build_taiex_daily_bars,
    parse_market_flows,
    parse_stock_flows,
    parse_taiex_index_history,
    parse_taiex_trading_volumes,
)

TRADE_DATE = date(2026, 9, 4)
FETCHED_AT = datetime(2026, 9, 7, tzinfo=UTC)

# Trimmed from the 2026-09-04 responses; the 合計 row and the aggregate T86
# columns are present so the parser is seen skipping them.
MARKET_PAYLOAD = {
    "stat": "OK",
    "date": "20260904",
    "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
    "data": [
        ["自營商(自行買賣)", "10,206,863,465", "8,700,559,652", "1,506,303,813"],
        ["自營商(避險)", "28,556,071,254", "23,692,313,823", "4,863,757,431"],
        ["投信", "14,968,188,503", "15,879,054,966", "-910,866,463"],
        ["外資及陸資(不含外資自營商)", "362,796,136,864", "306,583,183,061", "56,212,953,803"],
        ["外資自營商", "0", "0", "0"],
        ["合計", "416,527,260,086", "354,855,111,502", "61,672,148,584"],
    ],
}
STOCK_FIELDS = [
    "證券代號",
    "證券名稱",
    "外陸資買進股數(不含外資自營商)",
    "外陸資賣出股數(不含外資自營商)",
    "外陸資買賣超股數(不含外資自營商)",
    "外資自營商買進股數",
    "外資自營商賣出股數",
    "外資自營商買賣超股數",
    "投信買進股數",
    "投信賣出股數",
    "投信買賣超股數",
    "自營商買賣超股數",
    "自營商買進股數(自行買賣)",
    "自營商賣出股數(自行買賣)",
    "自營商買賣超股數(自行買賣)",
    "自營商買進股數(避險)",
    "自營商賣出股數(避險)",
    "自營商買賣超股數(避險)",
    "三大法人買賣超股數",
]
STOCK_ROW = [
    "2324",
    "仁寶            ",
    "91,160,046",
    "21,793,762",
    "69,366,284",
    "0",
    "0",
    "0",
    "0",
    "21,000",
    "-21,000",
    "1,671,047",
    "559,115",
    "359,000",
    "200,115",
    "2,071,402",
    "600,470",
    "1,470,932",
    "71,016,331",
]
STOCK_PAYLOAD = {
    "stat": "OK",
    "date": "20260904",
    "fields": STOCK_FIELDS,
    "data": [STOCK_ROW],
    "selectType": "ALLBUT0999",
}
# Verbatim TWSE message, fullwidth comma included.
NO_DATA_PAYLOAD = {"stat": "很抱歉，沒有符合條件的資料!"}  # noqa: RUF001
# What a morning run gets when it asks for today before TWSE publishes.
UNPUBLISHED_PAYLOAD = {"stat": "查詢日期大於可查詢最大日期！"}  # noqa: RUF001


def test_market_flows_parse_by_label_and_skip_the_total_row() -> None:
    flows = parse_market_flows(MARKET_PAYLOAD, trade_date=TRADE_DATE, fetched_at=FETCHED_AT)
    by_type = {item.investor_type: item for item in flows.items}
    assert set(by_type) == {"dealer_self", "dealer_hedge", "trust", "foreign", "foreign_dealer"}
    assert by_type["foreign"].net_amount == 56_212_953_803
    assert by_type["trust"].net_amount == -910_866_463


def test_stock_flows_unpivot_one_security_into_five_investors() -> None:
    flows = parse_stock_flows(STOCK_PAYLOAD, trade_date=TRADE_DATE, fetched_at=FETCHED_AT)
    assert len(flows.items) == 5
    assert {item.security_name for item in flows.items} == {"仁寶"}
    by_type = {item.investor_type: item for item in flows.items}
    assert by_type["foreign"].net_shares == 69_366_284
    assert by_type["trust"].net_shares == -21_000
    assert by_type["dealer_hedge"].buy_shares == 2_071_402


@pytest.mark.parametrize("payload", [NO_DATA_PAYLOAD, UNPUBLISHED_PAYLOAD], ids=["closed", "early"])
def test_no_data_stat_is_an_empty_day_not_an_error(payload: dict[str, object]) -> None:
    assert parse_market_flows(payload, trade_date=TRADE_DATE, fetched_at=FETCHED_AT).items == ()
    assert parse_stock_flows(payload, trade_date=TRADE_DATE, fetched_at=FETCHED_AT).items == ()


@pytest.mark.parametrize(
    "payload",
    [
        {**MARKET_PAYLOAD, "stat": "查詢錯誤"},
        {**MARKET_PAYLOAD, "date": "20260903"},
        {**MARKET_PAYLOAD, "data": [["新分類", "1", "1", "0"]]},
        {**MARKET_PAYLOAD, "data": [["投信", "10", "4", "5"]]},
        {**MARKET_PAYLOAD, "fields": ["單位名稱", "買進金額"]},
        {**MARKET_PAYLOAD, "data": MARKET_PAYLOAD["data"][1:]},
    ],
    ids=[
        "unknown_stat",
        "date_mismatch",
        "unknown_label",
        "net_mismatch",
        "missing_field",
        "missing_investor",
    ],
)
def test_contract_drift_raises(payload: dict[str, object]) -> None:
    with pytest.raises(DataSourceContractError):
        parse_market_flows(payload, trade_date=TRADE_DATE, fetched_at=FETCHED_AT)


@pytest.mark.asyncio
async def test_adapter_spaces_requests_and_maps_transport_errors() -> None:
    requests: list[httpx.Request] = []
    clock = {"now": 100.0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("T86"):
            return httpx.Response(200, json=STOCK_PAYLOAD)
        if request.url.params["dayDate"] == "20260905":
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=MARKET_PAYLOAD)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://twse.invalid"
    )
    async with TwseAdapter(
        base_url="https://twse.invalid",
        request_interval_seconds=6.0,
        client=client,
        sleep=sleep,
        monotonic=lambda: clock["now"],
    ) as adapter:
        stock = await adapter.get_stock_flows(TRADE_DATE)
        clock["now"] += 1.5
        market = await adapter.get_market_flows(TRADE_DATE)
        with pytest.raises(DataSourceTransientError):
            await adapter.get_market_flows(date(2026, 9, 5))
        await adapter.get_stock_flows(TRADE_DATE)

    assert len(stock.items) == 5 and len(market.items) == 5
    # The first request is immediate; each later one waits out the remainder of
    # the interval since the previous request finished. The 503 date is tried
    # three times and each failure adds another interval, so the waits grow
    # 6, 12, 18 and the request after it waits 24.
    assert sleeps == [4.5, 6.0, 12.0, 18.0, 24.0]
    assert sum(1 for request in requests if request.url.params.get("dayDate") == "20260905") == 3
    assert requests[0].url.params["selectType"] == "ALLBUT0999"
    assert requests[1].url.params["type"] == "day"


MONTH = date(2026, 9, 1)
# Trimmed from the 2026-09-10 responses. Both reports answer with a whole
# calendar month; the closing index appears in each, which is what lets the
# join check itself.
TAIEX_INDEX_PAYLOAD = {
    "stat": "OK",
    "title": "2026/09 TAIEX Total Index Historical Data",
    "date": "20260901",
    "fields": ["Date", "Opening Index", "Highest Index", "Lowest Index", "Closing Index"],
    "data": [
        ["2026/09/01", "46,177.11", "46,948.72", "46,081.11", "46,948.72"],
        ["2026/09/02", "46,901.32", "46,946.60", "46,164.72", "46,164.72"],
    ],
}
TAIEX_TRADING_PAYLOAD = {
    "stat": "OK",
    "title": "2026/09 Highlights of Daily Trading",
    "date": "20260901",
    "fields": ["Date", "Trade Volume", "Trade Value", "Transaction", "TAIEX", "Change"],
    "data": [
        ["2026/09/01", "13,000,849,196", "1,187,571,567,117", "5,301,801", "46,948.72", "820.25"],
        ["2026/09/02", "10,824,863,832", "976,499,979,054", "5,093,402", "46,164.72", "-784.00"],
    ],
}


def _taiex_bars(
    index_payload: Mapping[str, object] | None = None,
    trading_payload: Mapping[str, object] | None = None,
) -> TaiexDailyBars:
    return build_taiex_daily_bars(
        month=MONTH,
        index_history=parse_taiex_index_history(index_payload or TAIEX_INDEX_PAYLOAD, month=MONTH),
        trading_volumes=parse_taiex_trading_volumes(
            trading_payload or TAIEX_TRADING_PAYLOAD, month=MONTH
        ),
        fetched_at=FETCHED_AT,
    )


def test_taiex_joins_prices_from_one_report_with_volume_from_the_other() -> None:
    bars = _taiex_bars()

    assert [bar.trade_date for bar in bars.items] == [date(2026, 9, 1), date(2026, 9, 2)]
    first = bars.items[0]
    assert first.open == Decimal("46177.11")
    assert first.high == Decimal("46948.72")
    assert first.low == Decimal("46081.11")
    assert first.close == Decimal("46948.72")
    # The exchange publishes shares; Yahoo's ^TWII volume did not agree with it.
    assert first.volume == 13_000_849_196


def test_taiex_keeps_a_session_the_trading_report_has_not_published() -> None:
    # volume is nullable, so a bar that is otherwise complete is still worth
    # storing rather than losing the whole month over.
    trading = {**TAIEX_TRADING_PAYLOAD, "data": TAIEX_TRADING_PAYLOAD["data"][:1]}
    bars = _taiex_bars(trading_payload=trading)

    assert [bar.volume for bar in bars.items] == [13_000_849_196, None]


def test_taiex_rejects_reports_that_disagree_on_the_close() -> None:
    # Both reports carry the closing index. Disagreement means the two requests
    # straddled a correction, or a column moved; either way the month is not
    # trustworthy and must not be written.
    trading = {
        **TAIEX_TRADING_PAYLOAD,
        "data": [["2026/09/01", "13,000,849,196", "1", "1", "46,000.00", "0"]],
    }
    with pytest.raises(DataSourceContractError, match="trading highlights"):
        _taiex_bars(trading_payload=trading)


def test_taiex_rejects_a_row_from_another_month() -> None:
    index = {
        **TAIEX_INDEX_PAYLOAD,
        "data": [["2026/08/31", "46,177.11", "46,948.72", "46,081.11", "46,948.72"]],
    }
    with pytest.raises(DataSourceContractError, match="outside the requested month"):
        _taiex_bars(index_payload=index)


def test_taiex_month_in_the_future_is_empty_rather_than_an_error() -> None:
    # The English views answer an unpublished month with a stat sentence.
    future = {"stat": "Search date greater than today, please retry!", "total": 0}
    assert parse_taiex_index_history(future, month=MONTH) == {}
    assert parse_taiex_trading_volumes(future, month=MONTH) == {}


def test_taiex_rejects_a_report_that_is_not_the_taiex_index() -> None:
    # Neither report labels its rows with a symbol, and we store them under
    # ^TWII. If that path ever starts serving a different index, this is what
    # keeps it out of the series.
    other = {**TAIEX_INDEX_PAYLOAD, "title": "2026/09 Some Other Index Historical Data"}
    with pytest.raises(DataSourceContractError, match="not a TAIEX report"):
        _taiex_bars(index_payload=other)
