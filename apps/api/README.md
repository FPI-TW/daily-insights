# Daily Insights API

FastAPI modular monolith for identity, tenancy, market policy, conversations,
global model configuration, and shared R2 asset metadata.

## Boundaries

- This service owns application identities, organizations, authorization facts,
  report/chat metadata, derived outputs, and asset metadata.
- Raw market-data providers remain external. Provider adapters must translate their
  contracts; raw provider rows are not persisted here.
- R2 stores bytes. PostgreSQL stores object metadata and audit facts.
- Model selection is global. Organizations and users cannot override it.
- Missing organization-market policy rows mean **visible**. A row is an audited
  contractual override.

No legacy database or object migration is part of this fresh-start schema.

## Local commands

```bash
uv sync
uv run ruff format .
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run alembic upgrade head
uv run uvicorn daily_insights_api.main:app --reload
```

`DAILY_INSIGHTS_DATABASE_URL` defaults to a local PostgreSQL URL only when
`DAILY_INSIGHTS_ENVIRONMENT` is `development` or `test`. Staging and production
must provide it explicitly.

Health endpoints:

- `GET /api/health/live` — process liveness;
- `GET /api/health/ready` — database-aware readiness, returning `503` when the
  database is unavailable.

Equivalent unprefixed endpoints are available only for internal container
health checks and are omitted from OpenAPI.

Generate and validate the migration without a database:

```bash
uv run alembic upgrade head --sql
```
