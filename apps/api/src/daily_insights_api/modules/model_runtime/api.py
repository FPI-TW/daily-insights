import uuid
from dataclasses import dataclass
from typing import Protocol

from daily_insights_api.modules.model_runtime.models import (
    ActiveModelConfiguration as ActiveModelConfigurationRecord,
)
from daily_insights_api.modules.model_runtime.models import (
    GenerationRecord,
    ModelConfiguration,
)

CHAT_PROMPT_VERSION = "page-context.cross-market.v5"

__all__ = [
    "CHAT_PROMPT_VERSION",
    "ActiveModelConfiguration",
    "ActiveModelConfigurationRecord",
    "GenerationRecord",
    "ModelConfiguration",
    "ModelConfigurationReader",
]


@dataclass(frozen=True)
class ActiveModelConfiguration:
    configuration_id: uuid.UUID
    version: int
    provider: str
    requested_model: str
    prompt_version: str
    parameters: dict[str, object]


class ModelConfigurationReader(Protocol):
    async def active_configuration(self) -> ActiveModelConfiguration: ...
