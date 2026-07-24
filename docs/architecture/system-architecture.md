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
  customer UI, report/chart rendering, chat, internal back office

apps/api
  one deployable process, one transaction boundary, one migration graph
  modules/
    identity       credentials, sessions, password recovery
    tenancy        organizations, memberships, roles
    market_policy  catalog and organization visibility
    reports        report versions and localized presentation contracts
    data_sources   provider adapters and normalized source DTOs
    chat           conversations, messages, generation records, SSE
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

## Core domain ownership

| Module        | Owns                                                                       | Does not own                       |
| ------------- | -------------------------------------------------------------------------- | ---------------------------------- |
| Identity      | user credentials, session lifecycle                                        | organization permissions           |
| Tenancy       | organizations, memberships, role assignment                                | authentication secrets             |
| Market policy | canonical market catalog, org visibility and audit reason                  | browser-only feature flags         |
| Data sources  | provider clients, normalization, source provenance                         | durable raw-provider warehouse     |
| Reports       | derived facts, report/publication versions, localized narrative/chart DTOs | provider-specific response models  |
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
  presentation fields for `zh-TW`, `zh-CN`, and `en`.
- R2 object keys are generated server-side. Metadata, authorization, checksum,
  MIME type, size, and lifecycle state live in PostgreSQL.

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
