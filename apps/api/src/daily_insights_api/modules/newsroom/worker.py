"""The long-running newsroom worker (spec D13).

The framework here only claims rows, runs handlers, and runs periodic tasks.
Each workstream contributes a ``register(runtime) -> Registration`` from the
module it owns; this file wires them together and must not hold stage logic.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.notifier import Notice, Notifier, build_notifier
from daily_insights_api.modules.newsroom.providers import (
    Embedder,
    JsonModel,
    OpenAICompatibleEmbedder,
    OpenAICompatibleJsonModel,
)
from daily_insights_api.modules.newsroom.queue import (
    Claim,
    FatalStageError,
    StageHandler,
    StageSpec,
    claim,
    run_claimed,
)

logger = logging.getLogger(__name__)

HEARTBEAT_PATH = Path("/tmp/newsroom-worker-heartbeat")


class _UnconfiguredModel:
    """Stands in for a provider whose key is unset; every call fails fatally."""

    def __init__(self, code: str) -> None:
        self._code = code

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise FatalStageError(self._code)

    async def embed(self, *args: Any, **kwargs: Any) -> Any:
        raise FatalStageError(self._code)


@dataclass
class Runtime:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    llm: JsonModel
    embedder: Embedder
    notifier: Notifier

    @classmethod
    def build(
        cls, settings: Settings, session_factory: async_sessionmaker[AsyncSession]
    ) -> "Runtime":
        llm: JsonModel = (
            OpenAICompatibleJsonModel.from_settings(settings)
            if settings.newsroom_llm_api_key is not None
            else _UnconfiguredModel("newsroom_llm_configuration_missing")
        )
        embedder: Embedder = (
            OpenAICompatibleEmbedder.from_settings(settings)
            if settings.newsroom_embedding_api_key is not None
            else _UnconfiguredModel("newsroom_embedding_configuration_missing")
        )
        return cls(settings, session_factory, llm, embedder, build_notifier(settings))

    async def aclose(self) -> None:
        for client in (self.llm, self.embedder, self.notifier):
            closer = getattr(client, "aclose", None)
            if closer is not None:
                await closer()


@dataclass(frozen=True)
class StageBinding:
    stage: StageSpec
    handler: StageHandler
    concurrency: int = 1
    # Optional SQL filter narrowing which due rows this binding claims.
    extra_filter: Any = None


@dataclass(frozen=True)
class PeriodicTask:
    name: str
    interval: timedelta
    run: Callable[[], Awaitable[None]]


@dataclass
class Registration:
    stages: list[StageBinding] = field(default_factory=list)
    periodic: list[PeriodicTask] = field(default_factory=list)


def collect_registrations(runtime: Runtime) -> Registration:
    """Every workstream's contribution; each ``register`` lives in its owner's module."""
    from daily_insights_api.modules.newsroom import analysis, publishing, translation, triage
    from daily_insights_api.modules.newsroom.ingestion import register as register_ingestion

    merged = Registration()
    for part in (
        register_ingestion(runtime),
        triage.register(runtime),
        analysis.register(runtime),
        translation.register(runtime),
        publishing.register(runtime),
    ):
        merged.stages.extend(part.stages)
        merged.periodic.extend(part.periodic)
    return merged


class NewsroomWorker:
    def __init__(self, runtime: Runtime, registration: Registration) -> None:
        self._runtime = runtime
        self._registration = registration

    def _heartbeat(self) -> None:
        try:
            HEARTBEAT_PATH.touch()
        except OSError:
            logger.warning("newsroom.heartbeat_failed", exc_info=True)

    async def _on_fatal(self, claim_: Claim, error: FatalStageError) -> None:
        await self._runtime.notifier.send(
            Notice(
                kind="stage_fatal",
                title=f"新聞管線 {claim_.stage.name} 階段發生無法重試的錯誤",
                lines=(f"錯誤代碼 {error.code}",),
                dedupe_key=f"fatal:{claim_.stage.name}:{error.code}",
            )
        )

    async def _stage_loop(self, binding: StageBinding) -> None:
        poll = self._runtime.settings.newsroom_worker_poll_seconds
        while True:
            try:
                async with self._runtime.session_factory() as database:
                    claims = await claim(database, binding.stage, extra_filter=binding.extra_filter)
                for item in claims:
                    outcome = await run_claimed(
                        self._runtime.session_factory,
                        item,
                        binding.handler,
                        on_fatal=self._on_fatal,
                    )
                    logger.info(
                        "newsroom.stage",
                        extra={
                            "stage": binding.stage.name,
                            "row": str(item.row_id),
                            "outcome": outcome,
                        },
                    )
            except Exception:
                logger.exception("newsroom.stage_loop_error", extra={"stage": binding.stage.name})
                claims = []
            self._heartbeat()
            if not claims:
                await asyncio.sleep(poll)

    async def _periodic_loop(self, task: PeriodicTask) -> None:
        while True:
            try:
                await task.run()
            except Exception:
                logger.exception("newsroom.periodic_error", extra={"task": task.name})
            self._heartbeat()
            await asyncio.sleep(task.interval.total_seconds())

    async def run(self) -> None:
        self._heartbeat()
        async with asyncio.TaskGroup() as group:
            for binding in self._registration.stages:
                for _ in range(binding.concurrency):
                    group.create_task(self._stage_loop(binding))
            for task in self._registration.periodic:
                group.create_task(self._periodic_loop(task))
