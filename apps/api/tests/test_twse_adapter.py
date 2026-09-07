from datetime import date
from decimal import Decimal

import httpx
import pytest

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.data_sources.errors import DataSourceContractError
from daily_insights_api.modules.data_sources.twelve_data.transport import RetryPolicy
from daily_insights_api.modules.data_sources.twse import TwseAdapter, TwseTransport
from daily_insights_api.web.app import create_app

T86_FIELDS = [
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


def adapter(payload: dict[str, object]) -> TwseAdapter:
    transport = TwseTransport(
        client=httpx.AsyncClient(
            base_url="https://twse.test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request, json=payload)
            ),
        ),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    return TwseAdapter(transport)


async def test_bfi82u_converts_ntd_to_hundred_millions_and_combines_dealers() -> None:
    source = adapter(
        {
            "stat": "OK",
            "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
            "data": [
                ["自營商(自行買賣)", "0", "0", "100,000,000"],
                ["自營商(避險)", "0", "0", "-50,000,000"],
                ["投信", "0", "0", "200,000,000"],
                ["外資及陸資(不含外資自營商)", "0", "0", "-300,000,000"],
                ["外資自營商", "0", "0", "0"],
                ["合計", "0", "0", "-50,000,000"],
            ],
        }
    )
    result = await source.get_daily_flow(date(2026, 9, 4))
    assert result.item is not None
    assert result.item.foreign == Decimal("-3")
    assert result.item.trust == Decimal("2")
    assert result.item.dealer == Decimal("0.5")
    assert result.item.total == Decimal("-0.5")
    assert result.provenance.provider == "twse"
    assert len(result.provenance.contract_hash) == 64


async def test_t86_converts_shares_to_decimal_lots() -> None:
    row = ["2330", "台積電"] + ["0"] * 17
    row[4] = "12,345"
    row[10] = "2,000"
    row[11] = "-500"
    row[18] = "13,845"
    source = adapter({"stat": "OK", "fields": T86_FIELDS, "data": [row]})
    result = await source.get_stock_flows(date(2026, 9, 4), locale="zh-hant")
    item = result.items[0]
    assert item.foreign_lots == Decimal("12.345")
    assert item.trust_lots == Decimal("2")
    assert item.dealer_lots == Decimal("-0.5")
    assert item.total_lots == Decimal("13.845")


async def test_t86_missing_required_field_raises_contract_error() -> None:
    source = adapter({"stat": "OK", "fields": T86_FIELDS[:-1], "data": []})
    with pytest.raises(DataSourceContractError, match="required fields"):
        await source.get_stock_flows(date(2026, 9, 4), locale="zh-hant")


def test_openapi_exposes_institutional_endpoints_with_decimal_strings() -> None:
    document = create_app(Settings(environment="test")).openapi()
    assert "/api/markets/tw/institutional-flows" in document["paths"]
    assert "/api/markets/tw/institutional-stocks" in document["paths"]
    flow = document["components"]["schemas"]["InstitutionalFlowPointResponse"]
    assert flow["properties"]["foreign"] == {
        "pattern": r"^-?\d+(?:\.\d+)?$",
        "title": "Foreign",
        "type": "string",
    }
