FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

ENV PATH="/app/.venv/bin:${PATH}"

# The orchestration worker screenshots TradingView's heatmap. Playwright's
# headless shell is a third of a full Chromium and is what headless launches
# use; it is installed system-wide so the unprivileged runtime user can load it.
# This layer sits before the source COPY on purpose: it depends only on the
# lockfile, so editing apps/api never re-downloads Chromium or its apt deps.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium-headless-shell \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

COPY apps/api ./
RUN uv sync --frozen --no-dev
RUN mkdir -p /var/spool/podcast-media && chown nobody:nogroup /var/spool/podcast-media

USER nobody
EXPOSE 8000

CMD ["uvicorn", "daily_insights_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
