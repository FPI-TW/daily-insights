import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter

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
from daily_insights_api.core.logging import configure_logging
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.admin.router import router as admin_router
from daily_insights_api.modules.analyst_viewpoints.admin import (
    router as analyst_viewpoints_admin_router,
)
from daily_insights_api.modules.analyst_viewpoints.router import router as analyst_viewpoints_router
from daily_insights_api.modules.assets.object_store import ObjectStore
from daily_insights_api.modules.assets.r2.store import R2ObjectStore
from daily_insights_api.modules.chat.api import router as chat_router
from daily_insights_api.modules.chat.provider import OpenAICompatibleChatProvider
from daily_insights_api.modules.data_management.router import router as data_management_router
from daily_insights_api.modules.identity.password_work import PasswordWork
from daily_insights_api.modules.identity.router import router as identity_router
from daily_insights_api.modules.markets.router import router as markets_router
from daily_insights_api.modules.model_runtime.service import sync_chat_model_configuration
from daily_insights_api.modules.news.router import router as news_router
from daily_insights_api.modules.operations.health import ReadinessReport, evaluate_readiness
from daily_insights_api.modules.podcasts.router import router as podcasts_router
from daily_insights_api.modules.reports.router import router as reports_router

ReadinessChecker = Callable[[], Awaitable[bool]]


class HealthResponse(BaseModel):
    status: str


def create_app(
    settings: Settings | None = None,
    readiness_checker: ReadinessChecker | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    object_store: ObjectStore | None = None,
) -> FastAPI:
    configure_logging()
    resolved_settings = settings or get_settings()
    engine: AsyncEngine | None = None
    if (
        object_store is None
        and resolved_settings.r2_endpoint_url is not None
        and resolved_settings.r2_access_key_id is not None
        and resolved_settings.r2_secret_access_key is not None
    ):
        object_store = R2ObjectStore.from_credentials(
            endpoint_url=resolved_settings.r2_endpoint_url,
            access_key_id=resolved_settings.r2_access_key_id.get_secret_value(),
            secret_access_key=resolved_settings.r2_secret_access_key.get_secret_value(),
        )

    if session_factory is None:
        engine = create_engine(resolved_settings)
        session_factory = create_session_factory(engine)

    if readiness_checker is None:
        assert engine is not None

        async def check_readiness() -> bool:
            assert engine is not None
            return await database_is_ready(engine)

        readiness_checker = check_readiness

    async def r2_runtime_is_ready() -> bool:
        return resolved_settings.environment in {"development", "test"} or object_store is not None

    async def provider_runtime_is_ready() -> bool:
        return (
            not resolved_settings.morning_reports_enabled
            or resolved_settings.twelve_data_api_key is not None
        )

    async def news_runtime_is_ready() -> bool:
        return (
            not resolved_settings.daily_news_enabled
            or resolved_settings.news_model_api_key is not None
        )

    async def analyst_viewpoints_runtime_is_ready() -> bool:
        return (
            not resolved_settings.analyst_viewpoints_enabled
            or resolved_settings.analyst_viewpoints_api_key is not None
        )

    async def chat_runtime_is_ready() -> bool:
        return not resolved_settings.chat_enabled or (
            resolved_settings.chat_model_api_key is not None
            and bool(resolved_settings.chat_model_api_key.get_secret_value().strip())
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            if resolved_settings.chat_enabled:
                async with session_factory.begin() as database:
                    await sync_chat_model_configuration(database, resolved_settings)
            yield
        finally:
            app.state.password_work.close()
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title=resolved_settings.app_name, lifespan=lifespan)
    app.state.password_work = PasswordWork(resolved_settings.login_password_workers)
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory
    app.state.object_store = object_store
    if resolved_settings.chat_enabled and resolved_settings.chat_model_api_key is not None:
        app.state.chat_provider = OpenAICompatibleChatProvider(
            base_url=resolved_settings.chat_model_api_base_url,
            api_key=resolved_settings.chat_model_api_key.get_secret_value(),
            max_output_tokens=resolved_settings.chat_max_output_tokens,
        )

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
        started_at = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            emit_event(
                "http.request.completed",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status_code=500,
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )
            raise
        response.headers["X-Request-ID"] = request_id
        emit_event(
            "http.request.completed",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
        )
        return response

    app.include_router(identity_router)
    app.include_router(admin_router)
    app.include_router(data_management_router)
    app.include_router(analyst_viewpoints_admin_router)
    app.include_router(analyst_viewpoints_router)
    app.include_router(markets_router)
    app.include_router(reports_router)
    app.include_router(news_router)
    app.include_router(chat_router)
    app.include_router(podcasts_router)

    @app.get("/health/live", response_model=HealthResponse, include_in_schema=False)
    @app.get("/api/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/health/ready",
        response_model=ReadinessReport,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessReport}},
        include_in_schema=False,
    )
    @app.get(
        "/api/health/ready",
        response_model=ReadinessReport,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessReport}},
    )
    async def ready(request: Request) -> ReadinessReport | JSONResponse:
        del request
        assert readiness_checker is not None
        report = await evaluate_readiness(
            {
                "database": readiness_checker,
                "twelve_data_configuration": provider_runtime_is_ready,
                "daily_news_configuration": news_runtime_is_ready,
                "analyst_viewpoints_configuration": analyst_viewpoints_runtime_is_ready,
                "chat_configuration": chat_runtime_is_ready,
                "r2_runtime": r2_runtime_is_ready,
            }
        )
        if report.status != "ok":
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=report.model_dump(),
            )
        return report

    return app
