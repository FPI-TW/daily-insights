# Target system architecture

## Deployment shape

```text
Browser
  |
Cloudflare DNS/TLS/WAF
  |
nginx (only public app/API origin)
  +-- /api/* ----------------------> modular-monolith API
  |                                    |
  +-- /* --------------------------> TanStack Start web
                                       |
                    +------------------+------------------+
                    |                  |                  |
              RDS PostgreSQL     FinDB/provider APIs    model provider
                    |
              derived data, identity, policy,
              content, conversations, audit

Authorized browser -- short-lived signed URL --> private R2 object
```

The signed R2 transfer is deliberately outside nginx: the API authorizes the
request and issues a short-lived, object-scoped URL, while the file bytes flow
directly between the browser and R2. R2 credentials never reach the browser.

## Monorepo boundaries

```text
apps/web
  customer Podcast/report/chat UI and internal back office

apps/api
  one deployable process, one transaction boundary, one migration graph
  modules/
    identity       credentials, sessions, password recovery
    tenancy        organizations, memberships, roles
    markets        catalog and organization visibility
    reports        report versions, localized presentation contracts, scheduler
    data_sources   provider adapters (Twelve Data, FinDB, yfinance) and normalized DTOs
    news           feed discovery, safe extraction, DeepSeek daily news editions
    podcasts       episode catalog, publication lifecycle, localized metadata
    chat           conversations, messages, generation records, SSE
    model_runtime  globally versioned model configuration
    assets         R2 metadata, authorization, signed URL lifecycle
    admin          privileged use cases composed from domain services
    audit          security and privileged-operation evidence
    operations     health/readiness and scheduled daily jobs

packages/api-client
  generated web-facing OpenAPI client and schemas

infra/nginx
  path routing, SSE transport behavior, request limits and forwarding headers
```

Modules may call another module only through its public application interface.
They do not import another module's persistence implementation or write its
tables. Cross-module workflows are coordinated in application services and
committed in one database transaction where atomicity matters.

Web and API are independently deployable services and do not share an
application env file. Web configuration contains only its runtime mode and API
origin. Database, provider, model, and R2 configuration belongs exclusively to
the API. The root env file is limited to local Compose infrastructure wiring.

## Core domain ownership

| Module        | Owns                                                                       | Does not own                       |
| ------------- | -------------------------------------------------------------------------- | ---------------------------------- |
| Identity      | user credentials, session lifecycle                                        | organization permissions           |
| Tenancy       | organizations, memberships, role assignment                                | authentication secrets             |
| Market policy | canonical market catalog, org visibility and audit reason                  | browser-only feature flags         |
| Data sources  | provider clients, normalization, source provenance                         | durable raw-provider warehouse     |
| Reports       | derived facts, report/publication versions, localized narrative/chart DTOs | provider-specific response models  |
| Podcasts      | episode identity, localized metadata, ordering and publication lifecycle   | R2 credentials or object bytes     |
| Chat          | message ordering, generation state/facts, streaming                        | mutable global model secret/config |
| Assets        | object metadata, categories, signed access, lifecycle                      | raw R2 credentials in clients      |
| Admin         | privileged workflows                                                       | duplicate authorization rules      |
| Audit         | immutable privileged/security events                                       | general application logs           |

## Data boundaries and invariants

- PostgreSQL is the system of record for application-owned state.
- Raw provider responses may exist in memory or a bounded operational cache,
  but are not a durable application dataset.
- A derived publication stores input provenance, provider/source version,
  `as_of`, formula/version metadata, and locale-independent values sufficient
  to explain which inputs and logic produced it.
- An organization market policy is deny-by-exception: absence of an explicit
  restriction means visible. Policy checks happen server-side for every
  affected read and generation context.
- Market and tenant identifiers are derived from authenticated membership, not
  trusted request headers supplied by a browser.
- `organizations.seat_limit` is a required positive contractual limit.
  Membership creation locks the organization/member-count decision and fails
  atomically when the limit is reached. Admin limit changes are audited with
  their contract reference and before/after values.
- Every existing membership consumes a seat even when its user is suspended.
  Only removing the membership releases the seat.
- Accounts are admin-provisioned; there is no public registration. A random
  temporary password is stored only as a hash and must be changed after the
  first successful login before normal application access is granted. Its
  plaintext is shown once to the creating admin for secure out-of-band
  delivery and is never retrievable.
- Global model configuration is versioned. A generation snapshots all facts
  needed for historical attribution; changing the active pointer never mutates
  past rows.
- User message plus pending generation must commit before any provider call.
  Provider retries are idempotent and cannot duplicate the user message.
- Report/chart APIs return stable locale-neutral values plus localized
  presentation fields for `zh-hant`, `zh-hans`, and `en`.
- R2 object keys are generated server-side. Metadata, authorization, checksum,
  MIME type, size, and lifecycle state live in PostgreSQL.
- Podcast episodes reference asset IDs rather than raw R2 keys. Publishing
  requires at least one active locale audio variant; playback authorization is
  checked before each short-lived URL is issued.
- Podcast audio locale is modeled on an episode-to-asset variant relation, not
  inferred from object keys. Locale resolution first uses an exact requested
  match, then checks `zh-hant` → `zh-hans` → `en` in order.
- Podcast episode identity is the admin-specified unique trading date.
- The privileged Podcast browser upload accepts one to three files per request.
  It uses the stable canonical key
  `podcasts/{trading-date}/audio/{locale}/podcast.{mp3|mp4}`. Replacing an
  existing locale requires explicit confirmation and its expected current
  version. A same-extension replacement overwrites the same stable key; an
  MP3/MP4 format change writes the new stable-extension key before deleting the
  replaced old-format key. Both paths update checksum and object metadata,
  increment the logical version, and are audited.
- Existing legacy Podcast objects use a separate migration path. Inventory must
  assign every source object an explicitly reviewed locale; the application
  does not infer locale from a legacy key. Each object is copied to an
  immutable, locale-aware target key containing the asset ID and registered
  with that verified locale only after checksum verification. This migration
  key scheme is not the browser-upload key scheme.
- Podcast playback progress is browser-owned state keyed by authenticated user,
  episode, and resolved audio locale. It is validated at the localStorage trust
  boundary and is not duplicated in PostgreSQL.
- R2 migration is copy/verify/cutover, not move-in-place. A manifest and
  reconciliation gate precede active mapping changes; old-path deletion is a
  separate manual operation after verified cutover.

## External boundaries

### Raw-data providers

Each provider implements a narrow application-owned interface such as
instrument lookup, daily observations, and source metadata. The adapter:

1. validates upstream responses at the trust boundary;
2. maps them to normalized internal DTOs;
3. captures upstream request/correlation information without secrets;
4. exposes source availability and `as_of`;
5. fails closed when required data is missing.

Contract fixtures and scheduled compatibility checks tolerate additive fields
but alert on removed fields, type changes, or semantic/version changes. Adding
a provider means adding an adapter and mapping, not teaching reports about a
new vendor schema.

### Model providers

The chat application service resolves the one active global configuration,
persists its immutable version on the generation record, and then calls a
provider adapter. Secrets remain in the runtime secret store, never the
database record or audit log.

### R2

All management operations pass through the authenticated API. For downloads,
the API authorizes the user and returns a short-lived signed URL scoped to one
private object. Public, indefinitely cacheable brand assets may be considered
later, but private-by-default is the baseline.

## Security and operations

- nginx is transport routing, not identity or authorization. The API owns
  session validation, CSRF protection, RBAC, tenant isolation, and market
  policy.
- Prefer secure, HTTP-only, same-site session cookies with rotation and
  revocation over browser-managed bearer tokens.
- The initial release uses out-of-band temporary-password delivery and has no
  email or self-service forgot-password dependency. A later security milestone
  adds password recovery and mandatory MFA for `admin`; its email delivery uses
  Amazon SES in `ap-southeast-1`.
- Health endpoints distinguish liveness from readiness. Readiness includes the
  database and critical startup configuration, not every optional upstream.
- Structured logs carry a request ID from nginx through API calls. Metrics
  cover latency, errors, SSE connections, provider freshness, daily job
  outcomes, database saturation, and signed-URL issuance.
- Backups, restore tests, secrets rotation, host patching, and alert response
  are operational requirements, not deferred application features.
