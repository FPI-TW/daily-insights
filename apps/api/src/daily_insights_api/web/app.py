import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings, get_settings
from daily_insights_api.core.database import (
    create_engine,
    create_session_factory,
    database_is_ready,
)
from daily_insights_api.modules.admin.router import router as admin_router
from daily_insights_api.modules.identity.router import router as identity_router
from daily_insights_api.modules.markets.router import router as markets_router

ReadinessChecker = Callable[[], Awaitable[bool]]


class HealthResponse(BaseModel):
    status: str


def create_app(
    settings: Settings | None = None,
    readiness_checker: ReadinessChecker | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    engine: AsyncEngine | None = None

    if session_factory is None:
        engine = create_engine(resolved_settings)
        session_factory = create_session_factory(engine)

    if readiness_checker is None:
        assert engine is not None

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
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory

    @app.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        supplied_request_id = request.headers.get("X-Request-ID")
        request_id = (
            supplied_request_id
            if supplied_request_id is not None
            and 1 <= len(supplied_request_id) <= 100
            and supplied_request_id.isascii()
            and supplied_request_id.isprintable()
            else str(uuid.uuid4())
        )
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(identity_router)
    app.include_router(admin_router)
    app.include_router(markets_router)

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
