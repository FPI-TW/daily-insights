import uuid
from dataclasses import dataclass
from typing import Protocol


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
