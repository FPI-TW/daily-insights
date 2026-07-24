import asyncio
import hashlib
import math
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

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
    max_delay_seconds: float = 5.0
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
class FinDBTransportResponse:
    content: bytes
    fetched_at: datetime
    response_digest: str
    request_id: str | None


class FinDBTransport:
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
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._api_key = api_key
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._random_value = random_value
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
        )

    async def __aenter__(self) -> "FinDBTransport":
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
    ) -> FinDBTransportResponse:
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                response = await self._client.get(
                    endpoint,
                    params=params,
                    headers={"X-API-Key": self._api_key.get_secret_value()},
                )
            except httpx.TransportError:
                if attempt == self._retry_policy.max_attempts:
                    raise DataSourceTransientError(
                        f"FinDB network failure for {endpoint} after "
                        f"{self._retry_policy.max_attempts} attempts"
                    ) from None
                await self._sleep(self._retry_delay(attempt))
                continue

            if response.status_code in {401, 403}:
                raise DataSourceAuthenticationError(f"FinDB rejected credentials for {endpoint}")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self._retry_policy.max_attempts:
                    raise DataSourceTransientError(
                        f"FinDB remained unavailable for {endpoint} "
                        f"after {attempt} attempts (status {response.status_code})"
                    )
                await self._sleep(self._retry_delay(attempt, response))
                continue
            if response.status_code != 200:
                raise DataSourceContractError(
                    f"FinDB returned unexpected status {response.status_code} for {endpoint}"
                )

            request_id = response.headers.get("x-request-id")
            if request_id is not None:
                request_id = request_id.strip()[:255] or None
            return FinDBTransportResponse(
                content=response.content,
                fetched_at=datetime.now(UTC),
                response_digest=hashlib.sha256(response.content).hexdigest(),
                request_id=request_id,
            )

        raise AssertionError("bounded FinDB request loop exited unexpectedly")

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
                    parsed_retry_after = float(retry_after)
                    if math.isfinite(parsed_retry_after) and parsed_retry_after >= 0:
                        delay = min(self._retry_policy.max_delay_seconds, parsed_retry_after)
                except ValueError:
                    pass
        jitter = delay * self._retry_policy.jitter_ratio * self._random_value()
        return float(min(self._retry_policy.max_delay_seconds, delay + jitter))
