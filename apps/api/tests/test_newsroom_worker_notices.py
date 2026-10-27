import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom import queue
from daily_insights_api.modules.newsroom.clock import TAIPEI
from daily_insights_api.modules.newsroom.notifier import LogNotifier, Notifier, SlackNotifier
from daily_insights_api.modules.newsroom.queue import Claim, RetryableStageError
from daily_insights_api.modules.newsroom.worker import NewsroomWorker, Registration, Runtime


def _worker(notifier: Notifier) -> NewsroomWorker:
    runtime = Runtime(
        settings=Settings(_env_file=None),
        session_factory=cast(Any, None),
        llm=cast(Any, None),
        embedder=cast(Any, None),
        notifier=notifier,
    )
    return NewsroomWorker(runtime, Registration())


def _claim(stage: queue.StageSpec) -> Claim:
    return Claim(stage=stage, row_id=uuid.uuid4(), attempt=1)


def _spent(stage: str, retry_after: timedelta | None) -> RetryableStageError:
    return RetryableStageError(
        f"{stage}_provider_usage_limit", retry_after=retry_after, counts_attempt=False
    )


async def test_a_spent_allowance_notice_says_when_it_resets() -> None:
    notifier = LogNotifier()
    before = datetime.now(UTC)

    await _worker(notifier)._on_wait(_claim(queue.TRIAGE), _spent("triage", timedelta(hours=3)))

    (notice,) = notifier.sent
    assert notice.kind == "provider_usage_limit"
    assert notice.title == "新聞管線 triage 階段的 OpenCode Go 額度已用完"
    assert notice.dedupe_key == "usage_limit:triage"
    reset, waiting, extra_usage, code = notice.lines
    assert reset.startswith("預計台北時間 ") and reset.endswith(" 恢復")
    shown = datetime.strptime(reset.split(" ", 1)[1].removesuffix(" 恢復"), "%Y-%m-%d %H:%M")
    expected = (before + timedelta(hours=3)).astimezone(TAIPEI).replace(tzinfo=None)
    assert timedelta(0) <= shown - expected.replace(second=0, microsecond=0) <= timedelta(minutes=1)
    assert waiting == "資料列留在佇列等待 不會用掉重試次數"
    assert "Extra Usage" in extra_usage
    assert code == "錯誤代碼 triage_provider_usage_limit"


async def test_without_retry_after_the_notice_gives_the_retry_interval() -> None:
    notifier = LogNotifier()

    await _worker(notifier)._on_wait(_claim(queue.ANALYSIS), _spent("analysis", None))

    assert notifier.sent[0].lines[0] == "回應未提供恢復時間 每 60 分鐘重試一次"


async def test_slack_hears_once_per_stage_per_hour() -> None:
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content)["text"])
        return httpx.Response(200)

    slack = SlackNotifier("https://hooks.slack.com/services/test")
    await slack.aclose()
    slack._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    worker = _worker(slack)

    for _ in range(3):
        await worker._on_wait(_claim(queue.TRIAGE), _spent("triage", timedelta(hours=1)))
    await worker._on_wait(_claim(queue.EMBED), _spent("embed", timedelta(hours=1)))

    assert [text.splitlines()[0] for text in posted] == [
        "*新聞管線 triage 階段的 OpenCode Go 額度已用完*",
        "*新聞管線 embed 階段的 OpenCode Go 額度已用完*",
    ]
