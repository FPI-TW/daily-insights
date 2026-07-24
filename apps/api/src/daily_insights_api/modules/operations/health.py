from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel

ComponentCheck = Callable[[], Awaitable[bool]]


class ComponentHealth(BaseModel):
    status: Literal["ok", "unhealthy"]


class ReadinessReport(BaseModel):
    status: Literal["ok", "unhealthy"]
    components: dict[str, ComponentHealth]


async def evaluate_readiness(checks: dict[str, ComponentCheck]) -> ReadinessReport:
    components: dict[str, ComponentHealth] = {}
    for name, check in checks.items():
        try:
            healthy = await check()
        except Exception:
            healthy = False
        components[name] = ComponentHealth(status="ok" if healthy else "unhealthy")
    overall = "ok" if all(item.status == "ok" for item in components.values()) else "unhealthy"
    return ReadinessReport(status=overall, components=components)
