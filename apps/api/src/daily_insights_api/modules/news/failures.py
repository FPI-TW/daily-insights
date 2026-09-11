"""Typed news failure policy. No provider messages, credentials or bodies escape here."""

import socket
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import OperationalError

NewsStage = Literal["queued", "feed", "article", "selection", "summary", "publication", "complete"]
FailureAction = Literal["retry", "block", "repair", "skip", "attention", "expired", "cancelled"]


class NewsFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    stage: NewsStage
    action: FailureAction
    scope: str = "workflow"
    http_status: int | None = None
    retry_after: datetime | None = None
    candidate_id: str | None = None
    locale: str | None = None
    request_id: str | None = None


class NewsOperationError(Exception):
    def __init__(self, failure: NewsFailure) -> None:
        super().__init__(failure.code)
        self.failure = failure


def parse_retry_after(value: str | None, now: datetime) -> datetime | None:
    if not value:
        return None
    try:
        if value.strip().isdigit():
            # Avoid datetime overflow without shortening a meaningful cooldown.
            return now + timedelta(seconds=min(int(value.strip()), 365 * 86400))
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(now, parsed.astimezone(UTC))
    except (TypeError, ValueError, OverflowError):
        return None


def retry_time(attempt: int, now: datetime, retry_after: datetime | None = None) -> datetime:
    due = now + timedelta(minutes=(5, 15, 30)[min(max(attempt, 0), 2)])
    return max(due, retry_after) if retry_after is not None else due


def classify_failure(
    error: Exception,
    *,
    stage: NewsStage,
    scope: str = "workflow",
    candidate_id: str | None = None,
    locale: str | None = None,
    now: datetime | None = None,
) -> NewsFailure:
    if isinstance(error, NewsOperationError):
        return error.failure.model_copy(
            update={
                "stage": stage,
                "candidate_id": candidate_id or error.failure.candidate_id,
                "locale": locale or error.failure.locale,
            }
        )
    now = now or datetime.now(UTC)
    status: int | None = None
    after: datetime | None = None
    request_id: str | None = None
    code = "unexpected_error"
    action: FailureAction = "attention"
    is_model = stage in {"selection", "summary"}
    model_code = getattr(error, "error_code", None)
    response = error.response if isinstance(error, httpx.HTTPStatusError) else None
    if response is not None:
        status = response.status_code
        after = parse_retry_after(response.headers.get("retry-after"), now)
        request_id = response.headers.get("x-request-id")
    elif isinstance(model_code, str):
        if model_code.startswith("provider_http_"):
            try:
                status = int(model_code.removeprefix("provider_http_"))
            except ValueError:
                pass
        after = getattr(error, "retry_after", None)
        request_id = getattr(error, "request_id", None)
    if status is not None:
        code = f"provider_http_{status}" if is_model else f"source_http_{status}"
        if status == 429 or status == 408 or 500 <= status <= 599:
            action = "retry"
        elif is_model and status in {401, 402, 403}:
            action = "block"
        elif not is_model:
            action = "skip"
            if status in {404, 410}:
                code = "article_unavailable" if stage == "article" else "feed_unavailable"
            elif status in {401, 403}:
                code = "source_access_denied"
    elif isinstance(
        error,
        (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, socket.gaierror),
    ):
        code, action = (
            "request_timeout" if isinstance(error, httpx.TimeoutException) else "network_error",
            "retry",
        )
    elif isinstance(error, OperationalError):
        code, action = "database_unavailable", "retry"
    elif is_model and model_code in {
        "provider_invalid_json",
        "model_output_invalid",
        "selection_invalid_json",
        "selection_invalid_candidate",
        "summary_invalid_json",
        "summary_ungrounded_number",
    }:
        code, action = str(model_code), "repair"
    elif is_model and model_code == "provider_request_failed":
        # Preserve the underlying transport category; unknown errors never become retries.
        if isinstance(
            error.__cause__, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
        ):
            code, action = "provider_request_failed", "retry"
    elif not is_model and isinstance(error, DefusedXmlException):
        code, action = "unsafe_feed_xml", "skip"
    elif not is_model and isinstance(error, ParseError):
        code, action = "feed_parse_error", "skip"
    elif not is_model and isinstance(error, ValueError):
        # These are our own extraction errors, never displayed verbatim.
        message = str(error).lower()
        action = "skip"
        code = (
            "robots_denied"
            if "robots" in message
            else "response_too_large"
            if "size limit" in message or "exceeds" in message
            else "unsafe_destination"
            if any(
                term in message
                for term in ("allowlist", "public ip", "https", "unsafe", "redirect")
            )
            else "article_unreadable"
            if "short" in message or "paywall" in message
            else "unsupported_content"
            if "content type" in message
            else "feed_parse_error"
        )
    # The safe HTTP backend uses ConnectError for both policy rejection and a transport failure.
    if isinstance(error, httpx.ConnectError) and any(
        term in str(error).lower()
        for term in ("unsafe destination", "not exclusively public", "unix sockets")
    ):
        code, action = "unsafe_destination", "skip"
    if isinstance(error, socket.gaierror) and error.errno != socket.EAI_AGAIN:
        code, action = "source_dns_configuration", "skip"
    return NewsFailure(
        code=code,
        action=action,
        stage=stage,
        scope=scope,
        http_status=status,
        retry_after=after,
        candidate_id=candidate_id,
        locale=locale,
        request_id=request_id,
    )
