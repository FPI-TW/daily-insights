from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from daily_insights_api.core.config import Settings, get_settings
from daily_insights_api.core.database import create_engine, database_is_ready

ReadinessChecker = Callable[[], Awaitable[bool]]


class HealthResponse(BaseModel):
    status: str


def create_app(
    settings: Settings | None = None,
    readiness_checker: ReadinessChecker | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    engine: AsyncEngine | None = None

    if readiness_checker is None:
        engine = create_engine(resolved_settings)

        async def check_readiness() -> bool:
            assert engine is not None
            return await database_is_ready(engine)

        readiness_checker = check_readiness

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title=resolved_settings.app_name, lifespan=lifespan)

    @app.get("/health/live", response_model=HealthResponse, include_in_schema=False)
    @app.get("/api/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
        include_in_schema=False,
    )
    @app.get(
        "/api/health/ready",
        response_model=HealthResponse,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
    )
    async def ready(request: Request) -> HealthResponse | JSONResponse:
        del request
        assert readiness_checker is not None
        if not await readiness_checker():
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unhealthy"},
            )
        return HealthResponse(status="ok")

    return app
