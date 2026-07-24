# Implementation and acceptance roadmap

Phases are ordered by dependency. A later phase starts only after the prior
phase's acceptance evidence is recorded; parallel work is allowed within a
phase when file ownership does not overlap.

## Phase 0 — foundation and contracts

Deliver:

- monorepo workspaces for web, API, and API client;
- architecture decisions, environment contract, nginx and local Compose;
- API health/readiness and initial OpenAPI conventions;
- CI gates for format, lint, type checking, tests, and builds.

Accept when:

- `docker compose config` and `nginx -t` pass in the supported development
  environment;
- only nginx has a host port and the database is internal-only;
- web/API health checks converge;
- no committed environment file contains a real credential;
- architectural open items have owners and blocking phases.

## Phase 1 — identity, tenancy, and policy

Deliver:

- user, organization, membership, invitation/reset, and session models;
- `admin`, `asset_manager`, and `org_member` authorization;
- canonical eight-market catalog and default-visible org restrictions;
- admin organization/member/policy API and audit events.

Accept when:

- cross-organization access and spoofed tenant/market headers are rejected;
- customers cannot mutate organization or market policy;
- policy is enforced in API fixtures across all eight markets;
- admin changes include actor, reason, timestamp, and before/after evidence;
- session rotation, revocation, CSRF, password, and rate-limit tests pass.

Blocking decisions: membership/seat-limit behavior, email provider, and MFA
policy.

## Phase 2 — provider and report pipeline

Deliver:

- FinDB adapter and normalized DTOs;
- daily orchestration with idempotency, freshness, stale/error states, and
  alerts;
- versioned derived publications, provenance, chart contracts, and localized
  content;
- customer report/chart API and UI without PDF as the primary report.

Accept when:

- contract tests detect breaking FinDB fixture changes;
- no provider raw row is retained durably;
- repeated daily jobs cannot duplicate a publication;
- incomplete input cannot create a new publication;
- last published data remains readable and visibly stale during an outage;
- all report and chart surfaces honor org market policy and three locales.

Blocking decisions: formulas/derived indicators, editorial workflow, and final
FinDB semantics.

## Phase 3 — chat and model operations

Deliver:

- durable conversation/message/generation state machine;
- DeepSeek adapter behind a model-provider interface;
- globally versioned active model configuration;
- SSE delivery through nginx;
- admin-only conversation viewer and audit trail.

Accept when:

- user message and pending generation exist before a mocked provider receives
  the call;
- completed, partial, failed, timeout, retry, and disconnect paths preserve a
  coherent record without duplicate messages;
- each generation retains actual model/prompt/context/parameter/usage facts
  after a global model switch;
- customer APIs cannot enumerate prior conversations;
- admin viewing is authorized, scoped, and audited;
- load testing covers expected SSE concurrency and connection cleanup.

Blocking decisions: provider privacy/cross-border terms and PDPA retention
review.

## Phase 4 — shared asset administration

Deliver:

- R2 adapter, asset metadata, internal upload/update/download/lifecycle APIs;
- back-office UI for `admin` and `asset_manager`;
- private signed download URLs and common audio/image/PDF presentation.

Accept when:

- customers cannot upload or perform management operations;
- `asset_manager` cannot access organization, model, or conversation admin;
- object keys, MIME/size limits, authorization, checksum, and expiry are
  server-controlled;
- signed URLs are object-scoped, short-lived, and do not expose credentials;
- deleted/quarantined assets cannot be newly signed or displayed.

Blocking decisions: exact operations, upload limits, scanning, versioning, and
deletion/recovery.

## Phase 5 — production readiness and cutover

Deliver:

- EC2/RDS deployment automation and systemd-managed Compose/application
  lifecycle;
- Cloudflare DNS/TLS, AWS security groups, secrets, observability, alarms;
- database backup/restore and incident runbooks;
- capacity and failure testing; fresh production database/R2 namespace.

Accept when:

- EC2 replacement/reboot automatically restores healthy service;
- alarms reach the on-call destination during a controlled failure;
- an RDS point-in-time restore is timed and verified in isolation;
- no database or application origin port is publicly reachable except nginx
  from the approved Cloudflare/origin path;
- daily freshness and provider-failure alerts work;
- a load test demonstrates the agreed sub-1,000-user/SSE target with recorded
  headroom;
- rollback and DNS/origin cutover are rehearsed without touching legacy data.

There is no legacy-data migration or destructive legacy cleanup in this
roadmap.
