import asyncio
import hashlib
import math
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from pydantic import SecretStr

from daily_insights_api.modules.data_sources.errors import (
    DataSourceAuthenticationError,
    DataSourceContractError,
    DataSourceTransientError,
)

Sleep = Callable[[float], Awaitable[None]]
Random = Callable[[], float]
QueryValue = str | int | bool


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 10.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must not be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must not be less than base_delay_seconds")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class TwelveDataTransportResponse:
    content: bytes
    fetched_at: datetime
    response_digest: str
    request_id: str | None
    api_credits_used: int | None
    api_credits_left: int | None


class TwelveDataTransport:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        timeout_seconds: float = 10.0,
        retry_policy: RetryPolicy | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        random_value: Random = random.random,
        max_concurrency: int = 4,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= max_concurrency <= 20:
            raise ValueError("max_concurrency must be between 1 and 20")
        self._api_key = api_key
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._random_value = random_value
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
        )

    async def __aenter__(self) -> "TwelveDataTransport":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get(
        self,
        endpoint: str,
        *,
        params: Mapping[str, QueryValue],
    ) -> TwelveDataTransportResponse:
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                async with self._semaphore:
                    response = await self._client.get(
                        endpoint,
                        params=params,
                        headers={
                            "Authorization": f"apikey {self._api_key.get_secret_value()}",
                            "Accept": "application/json",
                        },
                    )
            except httpx.TransportError:
                if attempt == self._retry_policy.max_attempts:
                    raise DataSourceTransientError(
                        f"Twelve Data network failure for {endpoint} after "
                        f"{self._retry_policy.max_attempts} attempts"
                    ) from None
                await self._sleep(self._retry_delay(attempt))
                continue

            if response.status_code in {401, 403}:
                raise DataSourceAuthenticationError(
                    f"Twelve Data rejected credentials for {endpoint}"
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self._retry_policy.max_attempts:
                    raise DataSourceTransientError(
                        f"Twelve Data remained unavailable for {endpoint} "
                        f"after {attempt} attempts (status {response.status_code})"
                    )
                await self._sleep(self._retry_delay(attempt, response))
                continue
            if response.status_code != 200:
                raise DataSourceContractError(
                    f"Twelve Data returned unexpected status {response.status_code} for {endpoint}"
                )

            return TwelveDataTransportResponse(
                content=response.content,
                fetched_at=datetime.now(UTC),
                response_digest=hashlib.sha256(response.content).hexdigest(),
                request_id=_bounded_header(response, "x-request-id"),
                api_credits_used=_integer_header(response, "api-credits-used"),
                api_credits_left=_integer_header(response, "api-credits-left"),
            )

        raise AssertionError("bounded Twelve Data request loop exited unexpectedly")

    def _retry_delay(
        self,
        attempt: int,
        response: httpx.Response | None = None,
    ) -> float:
        delay = min(
            self._retry_policy.max_delay_seconds,
            self._retry_policy.base_delay_seconds * (2 ** (attempt - 1)),
        )
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after is not None:
                try:
                    parsed = float(retry_after)
                    if math.isfinite(parsed) and parsed >= 0:
                        delay = min(self._retry_policy.max_delay_seconds, parsed)
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                        if retry_at.tzinfo is not None:
                            delay = min(
                                self._retry_policy.max_delay_seconds,
                                max(0.0, (retry_at - datetime.now(UTC)).total_seconds()),
                            )
                    except (TypeError, ValueError):
                        pass
        jitter = delay * self._retry_policy.jitter_ratio * self._random_value()
        return float(min(self._retry_policy.max_delay_seconds, delay + jitter))


def _bounded_header(response: httpx.Response, name: str) -> str | None:
    value = response.headers.get(name)
    if value is None:
        return None
    return value.strip()[:255] or None


def _integer_header(response: httpx.Response, name: str) -> int | None:
    value = response.headers.get(name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
