FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY apps/api ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

USER nobody
EXPOSE 8000

CMD ["uvicorn", "daily_insights_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
