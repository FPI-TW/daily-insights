# Implementation and acceptance roadmap

Phases are ordered by dependency. A later phase starts only after the prior
phase's acceptance evidence is recorded; parallel work is allowed within a
phase when file ownership does not overlap.

## Current delivery priority

The immediate goal is to launch the Podcast pilot before expanding the product.
Work proceeds in this order:

1. separate the customer and administration entry points, authentication
   routes, layouts, redirects, and authorization boundaries;
2. finish the customer Podcast list and listening experience;
3. retain a focused administration interface for the current Podcast audio
   management workflow;
4. begin Phase 4 production deployment and cutover immediately after the
   revised Phase 3 acceptance criteria pass.

Phase 5 chat and Phase 6 product surfaces must not begin in a way that delays
the initial production launch. Customer and administration routes may remain in
one deployable web application for the initial release, but their navigation,
presentation, login entry points, and authorization behavior must be
independent.

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
read API are implemented. Formal eight-market content and the customer report
UI are explicitly pending and move to Phase 6B. Production scheduling and
alerts remain production-readiness work. See
[`phase-2-data-reports.md`](phase-2-data-reports.md).

Deliver:

- FinDB adapter and normalized DTOs;
- daily orchestration primitives with idempotency, fencing, freshness, and
  stale/error states;
- versioned derived publications, provenance, chart contracts, and localized
  content;
- policy-aware customer report/chart read API.

Accept when:

- contract tests detect breaking FinDB fixture changes;
- no provider raw row is retained durably;
- repeated daily jobs cannot duplicate a publication;
- incomplete input cannot create a new publication;
- last published data remains readable and visibly stale during an outage;
- the report read API honors org market policy and three locales.

Blocking decisions: formulas/derived indicators, editorial workflow, and final
FinDB semantics.

## Phase 2B — complete application architecture

Implementation status: complete in the current Phase 2B change set. Acceptance
evidence and remaining production risks are recorded in
[`phase-2b-application-architecture.md`](phase-2b-application-architecture.md).
The shared application shell and authorization foundation are complete. The
explicit customer/admin login and presentation split is revised Phase 3 work.

Deliver:

- public application interfaces and dependency rules for identity, tenancy,
  markets, reports, data sources, podcasts, assets, chat, admin, audit, and
  operations;
- migration-backed foundation tables for podcast catalog/lifecycle, shared
  assets, model configuration, conversations/generations, and operational job
  metadata where not already present;
- locale-aware Podcast audio-variant mapping backed by verified canonical
  copies, unique trading-date identity, replacement versioning, and a
  browser-local playback-progress contract;
- idempotent R2 copy/verify/cutover tooling with a migration manifest,
  checksum reconciliation, dry-run output, and no automatic source deletion;
- R2 object-store interface, signed-media access contract, and production
  configuration validation without exposing credentials;
- generated or schema-checked API client boundary for the web application;
- authenticated TanStack Start application shell, customer/back-office route
  separation, three-locale routing, shared loading/error states, and permission
  guards;
- nginx contracts for ordinary API requests, signed-media authorization, and
  future SSE traffic;
- module-level observability events and health/readiness ownership.

Accept when:

- architecture tests reject forbidden imports and cross-module table writes;
- all foundation tables are covered by migration upgrade/check/downgrade tests;
- production startup fails closed when mandatory database, session, R2, or
  provider configuration is invalid;
- customer and back-office route trees cannot cross authorization boundaries;
- OpenAPI/client contract drift is detected in CI;
- no browser bundle contains database, provider, model, or R2 credentials;
- Podcast can be implemented through public module interfaces without adding a
  second service or bypassing authorization.

This phase does not implement report content, report UI, chat UI, general asset
administration, or Podcast presentation.

## Phase 3 — Podcast pilot

Implementation status: the core Podcast API, audio management workflow, and
customer playback path are implemented locally with PostgreSQL/fake-R2
integration coverage, deterministic mock-based Playwright browser E2E, and
isolated live R2 adapter QA. Customer and admin entry points, guards, layouts,
and the deliberately limited UI/UX scope below are now implemented locally.
Actual
browser playback against R2 (including CORS/range), signed URL expiry, live
endpoint authentication/upload, network failure, and production capacity
remain Phase 3 acceptance gaps or Phase 4 production-readiness work. Detailed
domain scope and existing evidence are recorded in
[`podcast-pilot.md`](podcast-pilot.md).

Deliver:

- a customer login route at `/$locale/login` and a separate administration
  login route at `/$locale/admin/login`, with independent post-login redirects
  and no cross-surface navigation;
- separate customer and administration layouts and route guards, while keeping
  a single deployable web application for the initial release;
- a polished, responsive customer experience limited to the Podcast list and
  listening controls, with clear loading, empty, failure, unavailable-audio,
  keyboard, and mobile states;
- a concise administration experience limited to the current Podcast audio
  management workflow for `admin`, plus the approved R2 maintenance workflow
  for `admin` and `asset_manager`;
- canonical path/filename-derived episode presentation with missing-locale
  visibility;
- customer Podcast list, cover artwork, and accessible audio playback in
  TanStack Start; no report, chat, subscription, favorites, or general content
  management UI is included;
- API-authorized, short-lived R2 media URLs with object-scoped access;
- browser upload of one to three locale files plus migration of existing R2
  objects to backend-generated canonical keys;
- draft/published lifecycle, date ordering, locale audio fallback, playback
  progress, and unavailable-media handling;
- audit evidence for privileged publication and asset changes.

Accept when:

- customer credentials enter through `/$locale/login`, administration
  credentials enter through `/$locale/admin/login`, and authentication expiry
  or authorization failure returns each audience to its own login route;
- customer and administration layouts have distinct navigation and
  presentation, and neither surface exposes links or controls belonging to the
  other;
- the customer interface presents a clear, responsive Podcast list and permits
  listening without requiring access to administration routes;
- the administration interface presents the existing audio upload,
  replacement, publication, and unpublication workflow without unrelated
  customer or future administration features;
- an unauthorized or suspended user cannot list an episode or obtain a media
  URL;
- customers cannot upload, publish, unpublish, or mutate Podcast content;
- `asset_manager` capabilities remain limited to the approved Podcast/asset
  workflow and cannot administer organizations or conversations;
- every published episode has at least one valid active locale audio asset;
- selecting a locale plays its matching variant when present and otherwise
  resolves by `zh-hant` → `zh-hans` → `en`;
- duplicate trading-date/locale replacement requires explicit confirmation,
  overwrites the stable backend-generated key, and increments the logical
  database version;
- legacy R2 cutover cannot occur until every manifest entry passes
  size/MIME/SHA-256 verification; old-path cleanup remains a manual post-cutover
  action;
- expired, quarantined, missing, or unpublished media cannot receive a new
  signed URL;
- the player works through a short-lived URL without proxying audio bytes
  through nginx/API, and R2 credentials never reach the browser;
- list/player states pass responsive, keyboard, loading, empty, and
  failure-state tests;
- deterministic browser E2E covers both login entry points, route-boundary
  redirects, customer playback, and the administration audio workflow.

## Phase 4 — Podcast pilot production readiness and cutover

Phase 4 starts immediately after the revised Phase 3 acceptance evidence is
recorded. The goal is to put the limited Podcast pilot online before starting
chat, report UI, or broader asset-management work.

Deliver:

- EC2/RDS deployment automation and systemd-managed Compose/application
  lifecycle;
- Cloudflare DNS/TLS, AWS security groups, secrets, observability, alarms;
- database backup/restore and incident runbooks;
- Podcast/R2 capacity and failure testing using a fresh production database,
  verified canonical copies of approved Podcast objects, and a dedicated prefix
  for all target objects.

Accept when:

- EC2 replacement/reboot automatically restores healthy service;
- alarms reach the on-call destination during a controlled failure;
- an RDS point-in-time restore is timed and verified in isolation;
- no database or application origin port is publicly reachable except nginx
  from the approved Cloudflare/origin path;
- Podcast metadata, signing failures, missing objects, and application health
  have owned alerts;
- a load test demonstrates the agreed sub-1,000-user target with recorded
  headroom for page reads and signed-media issuance;
- authentication, tenant isolation, three locales, back-office Podcast
  publication, and private audio playback pass the production smoke test;
- rollback and DNS/origin cutover are rehearsed without touching legacy data.

This phase launches only the approved Podcast pilot. Chat, eight-market report
content, report UI, and general asset administration do not block it and must
not delay cutover. There is no legacy-data migration or destructive legacy
cleanup.

## Phase 5 — chat and model operations

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

## Phase 6 — pending product surfaces

This phase remains pending until the relevant product/data decisions are
approved. Podcast delivery does not implicitly approve either workstream.

### Phase 6A — shared asset administration expansion

Deliver:

- extend the Podcast asset slice to general audio/image/PDF/downloadable-file
  management;
- complete internal upload/update/download/lifecycle back-office workflows;
- private signed download URLs and reusable asset presentation components.

Accept when:

- customers cannot upload or perform management operations;
- `asset_manager` cannot access organization, model, or conversation admin;
- object keys, MIME/size limits, authorization, checksum, and expiry are
  server-controlled;
- signed URLs are object-scoped, short-lived, and do not expose credentials;
- deleted/quarantined assets cannot be newly signed or displayed.

Blocking decisions: exact operations, upload limits, scanning, versioning, and
deletion/recovery.

### Phase 6B — eight-market report content and customer UI

Deliver:

- approved eight-market dataset mappings and provider coverage;
- versioned formulas/derived indicators and editorial workflow;
- customer report/chart interface using structured data rather than PDF as the
  primary presentation;
- complete three-locale report narrative, labels, units, and failure states.

Accept when:

- every market has approved source, cutoff, freshness, missing-data, and
  correction semantics;
- calculations are reproducible from versioned inputs and derivation rules;
- report/chart surfaces enforce tenant market policy and three locales;
- provider outage behavior keeps the last successful publication visibly
  stale without publishing incomplete data.

Blocking decisions: the open items in
[`phase-2-data-reports.md`](phase-2-data-reports.md).

## Confirmed post-initial-release security work

- Require MFA for every `admin` account, including recovery and factor-reset
  audit procedures.
- Add a secure forgot-password and password-reset flow; the initial release
  intentionally has no self-service recovery.
- Use Amazon SES in AWS Singapore (`ap-southeast-1`) for password-recovery and
  MFA-related email.
- Add SES delivery monitoring, bounce/complaint handling, and alerting before
  enabling the email-dependent flow.
