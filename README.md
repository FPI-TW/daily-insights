# Daily Insights

Daily Insights is the fresh-start monorepo for a multilingual market-reporting
product. The target system combines the existing report UI concepts with a new
modular-monolith API. Previous `report-*` services are reference material only;
their data and runtime contracts are not migrated into this workspace.

## Target workspace

```text
apps/
  web/              TanStack Start customer and internal-admin UI
  api/              Modular-monolith API
packages/
  api-client/       Generated or shared API contracts
docs/
  architecture/     Decisions, boundaries, and delivery roadmap
  runbooks/         Operational procedures
infra/
  nginx/            Development ingress foundation
```

See [system architecture](docs/architecture/system-architecture.md) and the
[implementation roadmap](docs/architecture/roadmap.md) before adding a domain
or service.

## Current foundation

The TanStack Start application lives in `apps/web`. The modular FastAPI
foundation lives in `apps/api`. Frontend workspace commands are available from
the repository root:

```bash
pnpm install
pnpm dev
pnpm format:check
pnpm lint:check
pnpm type:check
pnpm test
pnpm build
```

Do not infer that the product domains described in the architecture documents
are implemented merely because they appear in the target design.

## Development infrastructure foundation

Copy `.env.example` to `.env` and replace every placeholder before starting
the full stack:

```bash
docker compose config
docker compose up --build
```

The development ingress listens on `http://localhost:8080` by default. Only
nginx is exposed; PostgreSQL remains on the internal Compose network.

The Compose file is a development foundation, not the production topology.
Production guidance is in the
[Singapore deployment runbook](docs/runbooks/production.md).

## Architecture references

- [Confirmed decisions and open items](docs/architecture/product-decisions.md)
- [System and domain boundaries](docs/architecture/system-architecture.md)
- [Phased implementation and acceptance roadmap](docs/architecture/roadmap.md)
- [Production operations runbook](docs/runbooks/production.md)
