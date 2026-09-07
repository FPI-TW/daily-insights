import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from daily_insights_api.modules.data_sources.dto import (
    InstitutionalFlowDay,
    InstitutionalFlowResult,
    InstitutionalStockFlow,
    InstitutionalStockFlowResult,
    Provenance,
)
from daily_insights_api.modules.data_sources.errors import DataSourceContractError
from daily_insights_api.modules.data_sources.twse.schemas import TwseResponse
from daily_insights_api.modules.data_sources.twse.transport import TwseTransport

TWSE_CONTRACT_VERSION = "twse-institutional-v1"
BFI82U_ENDPOINT = "/rwd/zh/fund/BFI82U"
T86_ENDPOINTS = {
    "zh-hant": "/rwd/zh/fund/T86",
    "zh-hans": "/rwd/zh/fund/T86",
    "en": "/rwd/en/fund/T86",
}
BFI82U_FIELDS = ("單位名稱", "買進金額", "賣出金額", "買賣差額")
T86_FIELDS = (
    "證券代號",
    "證券名稱",
    "外陸資買賣超股數(不含外資自營商)",
    "投信買賣超股數",
    "自營商買賣超股數",
    "三大法人買賣超股數",
)
TWSE_CONTRACT_HASH = hashlib.sha256(
    json.dumps(
        {"BFI82U": BFI82U_FIELDS, "T86": T86_FIELDS},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
).hexdigest()
HUNDRED_MILLION = Decimal("100000000")
SHARES_PER_LOT = Decimal("1000")


class TwseAdapter:
    def __init__(self, transport: TwseTransport) -> None:
        self._transport = transport

    async def close(self) -> None:
        await self._transport.close()

    async def get_daily_flow(self, trade_date: date) -> InstitutionalFlowResult:
        response = await self._transport.get(
            BFI82U_ENDPOINT,
            params={"date": trade_date.strftime("%Y%m%d"), "response": "json"},
        )
        payload = _payload(response.content, "BFI82U")
        provenance = _provenance(
            endpoint=BFI82U_ENDPOINT,
            trade_date=trade_date,
            query={"date": trade_date.isoformat()},
            fetched_at=response.fetched_at,
            digest=response.response_digest,
            request_id=response.request_id,
            record_count=len(payload.data),
        )
        if payload.stat != "OK" or not payload.data:
            return InstitutionalFlowResult(item=None, provenance=provenance)
        _require_fields(payload.fields, BFI82U_FIELDS, "BFI82U")
        difference = payload.fields.index("買賣差額")
        rows = {row[0].strip(): row for row in payload.data if len(row) == len(payload.fields)}
        required_rows = (
            "自營商(自行買賣)",
            "自營商(避險)",
            "投信",
            "外資及陸資(不含外資自營商)",
            "合計",
        )
        if any(label not in rows for label in required_rows):
            raise DataSourceContractError("TWSE BFI82U required institution row is missing")
        dealer = _number(rows[required_rows[0]][difference]) + _number(
            rows[required_rows[1]][difference]
        )
        item = InstitutionalFlowDay(
            trade_date=trade_date,
            foreign=_number(rows[required_rows[3]][difference]) / HUNDRED_MILLION,
            trust=_number(rows[required_rows[2]][difference]) / HUNDRED_MILLION,
            dealer=dealer / HUNDRED_MILLION,
            total=_number(rows[required_rows[4]][difference]) / HUNDRED_MILLION,
        )
        return InstitutionalFlowResult(item=item, provenance=provenance)

    async def get_stock_flows(
        self, trade_date: date, *, locale: str
    ) -> InstitutionalStockFlowResult:
        endpoint = T86_ENDPOINTS.get(locale)
        if endpoint is None:
            raise DataSourceContractError(f"unsupported TWSE locale: {locale}")
        response = await self._transport.get(
            endpoint,
            params={
                "date": trade_date.strftime("%Y%m%d"),
                "selectType": "ALLBUT0999",
                "response": "json",
            },
        )
        payload = _payload(response.content, "T86")
        if payload.stat != "OK":
            items: tuple[InstitutionalStockFlow, ...] = ()
        else:
            if locale == "en":
                if len(payload.fields) < 19:
                    raise DataSourceContractError("TWSE T86 required fields are missing")
                indices = (0, 1, 4, 10, 11, 18)
            else:
                _require_fields(payload.fields, T86_FIELDS, "T86")
                indices = tuple(payload.fields.index(field) for field in T86_FIELDS)  # type: ignore[assignment]
            normalized: list[InstitutionalStockFlow] = []
            for row in payload.data:
                if len(row) != len(payload.fields):
                    raise DataSourceContractError("TWSE T86 row length does not match fields")
                symbol, name, foreign, trust, dealer, total = (row[index] for index in indices)
                normalized.append(
                    InstitutionalStockFlow(
                        trade_date=trade_date,
                        symbol=symbol.strip(),
                        name=name.strip(),
                        foreign_lots=_number(foreign) / SHARES_PER_LOT,
                        trust_lots=_number(trust) / SHARES_PER_LOT,
                        dealer_lots=_number(dealer) / SHARES_PER_LOT,
                        total_lots=_number(total) / SHARES_PER_LOT,
                    )
                )
            items = tuple(normalized)
        return InstitutionalStockFlowResult(
            items=items,
            provenance=_provenance(
                endpoint=endpoint,
                trade_date=trade_date,
                query={"date": trade_date.isoformat(), "selectType": "ALLBUT0999"},
                fetched_at=response.fetched_at,
                digest=response.response_digest,
                request_id=response.request_id,
                record_count=len(items),
            ),
        )


def _payload(content: bytes, report: str) -> TwseResponse:
    try:
        return TwseResponse.model_validate_json(content)
    except ValidationError as error:
        raise DataSourceContractError(f"TWSE {report} response failed validation") from error


def _require_fields(actual: list[str], required: tuple[str, ...], report: str) -> None:
    if any(field not in actual for field in required):
        raise DataSourceContractError(f"TWSE {report} required fields are missing")


def _number(value: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "").strip())
    except InvalidOperation as error:
        raise DataSourceContractError(f"TWSE numeric value is invalid: {value!r}") from error


def _provenance(
    *,
    endpoint: str,
    trade_date: date,
    query: dict[str, str],
    fetched_at: object,
    digest: str,
    request_id: str | None,
    record_count: int,
) -> Provenance:
    from datetime import datetime

    assert isinstance(fetched_at, datetime)
    fingerprint = hashlib.sha256(
        json.dumps(query, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return Provenance(
        provider="twse",
        contract_version=TWSE_CONTRACT_VERSION,
        contract_hash=TWSE_CONTRACT_HASH,
        endpoint=endpoint,
        query_fingerprint=fingerprint,
        fetched_at=fetched_at,
        as_of=trade_date,
        response_digest=digest,
        record_count=record_count,
        request_id=request_id,
    )
