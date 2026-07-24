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

Implementation status: complete in the current Phase 1 change set. PostgreSQL
acceptance coverage is documented in
[`phase-1-identity.md`](phase-1-identity.md).

Deliver:

- admin-provisioned user, organization, membership, password-change, and
  session models;
- `admin`, `asset_manager`, and `org_member` authorization;
- contractual seat-limit enforcement with audited renewal/addendum changes;
- canonical eight-market catalog and default-visible org restrictions;
- admin organization/member/policy API and audit events.

Accept when:

- cross-organization access and spoofed tenant/market headers are rejected;
- customers cannot mutate organization or market policy;
- policy is enforced in API fixtures across all eight markets;
- admin changes include actor, reason, timestamp, and before/after evidence;
- concurrent membership creation cannot exceed an organization's positive
  `seat_limit`;
- suspended users continue to consume a seat and only membership removal
  releases it;
- public registration is unavailable, temporary passwords are never persisted
  as plaintext or retrievable after their one-time display to the admin, and
  normal access is blocked until the initial password is changed;
- session rotation, revocation, CSRF, password, and rate-limit tests pass.

## Phase 2 — provider and report pipeline

Implementation status: foundation complete. The provider adapter, immutable
publication contract, database-backed orchestration primitives, and customer
read API are implemented. Formal eight-market content, production scheduling
and alerts, and the report UI remain blocked or scheduled for later work as
documented in [`phase-2-data-reports.md`](phase-2-data-reports.md).

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

## Confirmed post-initial-release security work

- Require MFA for every `admin` account, including recovery and factor-reset
  audit procedures.
- Add a secure forgot-password and password-reset flow; the initial release
  intentionally has no self-service recovery.
- Use Amazon SES in AWS Singapore (`ap-southeast-1`) for password-recovery and
  MFA-related email.
- Add SES delivery monitoring, bounce/complaint handling, and alerting before
  enabling the email-dependent flow.
