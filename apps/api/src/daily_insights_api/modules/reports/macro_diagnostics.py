"""Request-local, admin-only macro provider diagnostics. Never serialize raw errors."""

import json
import socket
from collections.abc import Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Literal
from xml.etree.ElementTree import ParseError

import httpx
from curl_cffi.requests import exceptions as curl_errors
from pydantic import BaseModel, Field, ValidationError
from yfinance.exceptions import YFRateLimitError

from daily_insights_api.modules.data_sources.api import (
    DataSourceAuthenticationError,
    DataSourceContractError,
)


class SourceFailure(BaseModel):
    affected_items: list[str]
    endpoint: str
    failure_type: str
    http_status: int | None = None


class SourceDiagnostic(BaseModel):
    code: str
    name: str
    status: Literal["ok", "degraded", "unavailable", "disabled"]
    fetched_at: datetime
    affected_items: list[str] = Field(default_factory=list)
    failures: list[SourceFailure] = Field(default_factory=list)


diagnostics: ContextVar[list[tuple[str, SourceFailure]] | None] = ContextVar(
    "macro_diagnostics", default=None
)


class EmptySourceResponse(ValueError):
    """A valid provider response contains no usable observations."""


def classify(error: BaseException) -> tuple[str, int | None]:
    """Inspect typed exceptions and their causes; never parse credential-bearing messages."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    for item in chain:
        status = getattr(item, "http_status", None)
        if isinstance(item, httpx.HTTPStatusError):
            status = item.response.status_code
        if isinstance(item, curl_errors.HTTPError) and item.response is not None:
            status = item.response.status_code
        if isinstance(item, YFRateLimitError):
            return "rate_limited", 429
        if isinstance(status, int):
            kind = (
                "rate_limited"
                if status == 429
                else ("authentication_error" if status in {401, 402, 403} else "http_error")
            )
            return kind, status
    for item in reversed(chain):
        if isinstance(item, (socket.gaierror, curl_errors.DNSError)):
            return "dns_error", None
        if isinstance(item, (httpx.TimeoutException, TimeoutError, curl_errors.Timeout)):
            return "timeout", None
        if isinstance(item, (httpx.TransportError, ConnectionError, curl_errors.ConnectionError)):
            return "connection_error", None
        if isinstance(item, (json.JSONDecodeError, ParseError)):
            return "parse_error", None
        if isinstance(item, DataSourceAuthenticationError):
            return "authentication_error", None
        if isinstance(item, EmptySourceResponse):
            return "empty_response", None
        if isinstance(item, (ValidationError, DataSourceContractError, ValueError)):
            return "validation_error", None
    return "unknown", None


def record_failure(
    source: str,
    endpoint: str,
    items: list[str],
    error: BaseException | None = None,
    *,
    kind: str = "empty_response",
) -> None:
    sink = diagnostics.get()
    if sink is None:
        return
    failure_type, status = classify(error) if error is not None else (kind, None)
    sink.append(
        (
            source,
            SourceFailure(
                affected_items=items,
                endpoint=endpoint,
                failure_type=failure_type,
                http_status=status,
            ),
        )
    )


def summarize(
    entries: list[tuple[str, SourceFailure]],
    states: Sequence[tuple[str, str, Sequence[tuple[str, str]]]],
    fetched_at: datetime | None = None,
) -> list[SourceDiagnostic]:
    results = []
    for code, name, items in states:
        failures = [failure for source, failure in entries if source == code]
        missing = [symbol for symbol, state in items if state == "unavailable"]
        known = {symbol for failure in failures for symbol in failure.affected_items}
        if unknown := [symbol for symbol in missing if symbol not in known]:
            failures.append(
                SourceFailure(
                    affected_items=unknown, endpoint="dataset", failure_type="empty_response"
                )
            )
        status: Literal["ok", "degraded", "unavailable", "disabled"] = "ok"
        if items and all(state == "disabled" for _, state in items):
            status = "disabled"
        elif items and all(state == "unavailable" for _, state in items):
            status = "unavailable"
        elif failures or any(state != "ok" for _, state in items):
            status = "degraded"
        results.append(
            SourceDiagnostic(
                code=code,
                name=name,
                status=status,
                fetched_at=fetched_at or datetime.now(UTC),
                affected_items=sorted(
                    {item for failure in failures for item in failure.affected_items}
                ),
                failures=failures,
            )
        )
    return results
