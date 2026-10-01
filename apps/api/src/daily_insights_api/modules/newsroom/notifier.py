"""Operator notifications (spec D17). Slack first; the interface allows swapping.

Notifications are best effort: a failed send is logged and never fails a stage.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx

from daily_insights_api.core.config import Settings

logger = logging.getLogger(__name__)

NoticeKind = Literal[
    "draft_ready",
    "selection_fallback",
    "source_unhealthy",
    "late_fill_abandoned",
    "stage_fatal",
    "publish_failed",
]


@dataclass(frozen=True, slots=True)
class Notice:
    kind: NoticeKind
    title: str
    lines: tuple[str, ...] = ()
    link: str | None = None
    # Notices sharing a dedupe key are sent at most once per ``dedupe_seconds``.
    dedupe_key: str | None = None


class Notifier(Protocol):
    async def send(self, notice: Notice) -> None: ...


@dataclass
class LogNotifier:
    """Used when no webhook is configured (local development, tests)."""

    sent: list[Notice] = field(default_factory=list)

    async def send(self, notice: Notice) -> None:
        self.sent.append(notice)
        logger.info("newsroom.notice", extra={"kind": notice.kind, "title": notice.title})


class SlackNotifier:
    def __init__(self, webhook_url: str, *, dedupe_seconds: float = 3600) -> None:
        self._webhook_url = webhook_url
        self._dedupe_seconds = dedupe_seconds
        self._last_sent: dict[str, float] = {}
        self._client = httpx.AsyncClient(
            timeout=10, follow_redirects=False, cookies=None, trust_env=False
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _deduped(self, notice: Notice) -> bool:
        if notice.dedupe_key is None:
            return False
        now = time.monotonic()
        last = self._last_sent.get(notice.dedupe_key)
        if last is not None and now - last < self._dedupe_seconds:
            return True
        self._last_sent[notice.dedupe_key] = now
        return False

    async def send(self, notice: Notice) -> None:
        if self._deduped(notice):
            return
        text = "\n".join(
            [
                f"*{notice.title}*",
                *notice.lines,
                *([f"<{notice.link}|開啟後台>"] if notice.link else []),
            ]
        )
        try:
            response = await self._client.post(self._webhook_url, json={"text": text})
            response.raise_for_status()
        except Exception:
            logger.warning("newsroom.notice_failed", extra={"kind": notice.kind}, exc_info=True)


def build_notifier(settings: Settings) -> Notifier:
    webhook = settings.newsroom_slack_webhook_url
    if webhook is None or not webhook.get_secret_value().strip():
        return LogNotifier()
    return SlackNotifier(webhook.get_secret_value().strip())
