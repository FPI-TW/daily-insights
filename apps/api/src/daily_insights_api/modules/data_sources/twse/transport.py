import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from daily_insights_api.modules.data_sources.errors import (
    DataSourceContractError,
    DataSourceTransientError,
)
from daily_insights_api.modules.data_sources.twelve_data.transport import RetryPolicy

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TwseTransportResponse:
    content: bytes
    fetched_at: datetime
    response_digest: str
    request_id: str | None


class TwseTransport:
    def __init__(
        self,
        *,
        base_url: str = "https://www.twse.com.tw",
        timeout_seconds: float = 10,
        retry_policy: RetryPolicy | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        max_concurrency: int = 4,
    ) -> None:
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=3)
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout_seconds)
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get(self, endpoint: str, *, params: Mapping[str, str]) -> TwseTransportResponse:
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                async with self._semaphore:
                    response = await self._client.get(
                        endpoint,
                        params=params,
                        headers={"Accept": "application/json"},
                    )
            except httpx.TransportError:
                if attempt == self._retry_policy.max_attempts:
                    raise DataSourceTransientError(
                        f"TWSE network failure for {endpoint} after {attempt} attempts"
                    ) from None
                await self._sleep(self._retry_delay(attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self._retry_policy.max_attempts:
                    raise DataSourceTransientError(
                        f"TWSE remained unavailable for {endpoint} after {attempt} attempts "
                        f"(status {response.status_code})"
                    )
                await self._sleep(self._retry_delay(attempt))
                continue
            if response.status_code != 200:
                raise DataSourceContractError(
                    f"TWSE returned unexpected status {response.status_code} for {endpoint}"
                )
            return TwseTransportResponse(
                content=response.content,
                fetched_at=datetime.now(UTC),
                response_digest=hashlib.sha256(response.content).hexdigest(),
                request_id=(response.headers.get("x-request-id") or "").strip()[:255] or None,
            )
        raise AssertionError("bounded TWSE request loop exited unexpectedly")

    def _retry_delay(self, attempt: int) -> float:
        return float(
            min(
                self._retry_policy.max_delay_seconds,
                self._retry_policy.base_delay_seconds * (2 ** (attempt - 1)),
            )
        )
