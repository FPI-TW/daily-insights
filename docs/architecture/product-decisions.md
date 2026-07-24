# Product decisions

Status: accepted product baseline for Phase 0.

This document separates confirmed product requirements from implementation
recommendations and genuinely unresolved product details. Recommendations are
not requirements until accepted.

## Confirmed decisions

### Product and tenancy

- This is a fresh-start project. Existing users, sessions, conversations,
  reports, PDFs, and R2 metadata are neither migrated nor deleted.
- An `Organization` represents a customer company. Users join an organization
  through membership; a customer does not administer its own organization.
- The internal back office has two privileged roles:
  - `admin`: organization and member CRUD, organization market visibility,
    shared asset management, model configuration, and conversation viewing.
  - `asset_manager`: shared R2 asset maintenance only.
- `org_member` is a customer-facing member with no back-office permissions.

### Eight-market policy

- The complete product comprises eight markets.
- Every market is visible by default.
- Internal `admin` users hide markets for an organization according to the
  business team's customer contract.
- Customers and their members cannot change this policy.
- Contract visibility changes are expected to be infrequent.
- Authorization is enforced by the API across reports, chart data, chat
  context, search, downloads, and administrative previews. Hiding navigation in
  the browser is not an authorization control.

The confirmed catalog is US macro/global bonds, cryptocurrency, foreign
exchange, US equities, Hong Kong equities, Mainland China equities, Taiwan
equities, and Taiwan index futures/options. Phase 1 seeds stable internal codes
and Traditional Chinese, Simplified Chinese, and English labels for all eight.

### Reports, data, and localization

- Primary reports are rendered from fetched structured data and charts, not
  presented as PDF documents.
- PDF remains an allowed downloadable asset format.
- Reports, chart labels, and narrative content support Traditional Chinese
  (`zh-TW`), Simplified Chinese (`zh-CN`), and English (`en`).
- FinDB is the first raw-data source and its contract may grow or change.
  Additional raw-data providers are expected.
- This application does not durably duplicate provider raw rows. It stores
  application-owned derived results, publication versions, provenance, and
  source `as_of` information.

### Conversations and models

- The initial provider is DeepSeek, but the provider/model may change.
- One globally active model configuration applies to all organizations.
  Customers cannot choose or override it.
- A global switch applies to subsequent generations; historical generation
  facts never follow the mutable current setting.
- Each assistant generation records provider, requested and resolved model,
  model-configuration version, prompt version, context/report versions,
  generation parameters, provider request ID, token usage, timing, status, and
  sanitized error metadata.
- The user message and a pending assistant-generation record are committed
  before calling the model. Completion transitions through
  `pending -> complete | partial | error`.
- Conversation history is retained indefinitely at launch. Customers cannot
  browse history, delete it, or export it. An `admin` can view it in the back
  office.

### Assets and deployment

- Cloudflare R2 stores internally managed audio and static/downloadable files,
  including images and PDFs.
- Assets are shared across customers. There is no customer upload capability.
- nginx replaces the previous gateway and is the single public application/API
  ingress.
- The production region is AWS Singapore (`ap-southeast-1`).
- Expected concurrency is below 1,000 users and data freshness is daily.
- Service interruption is acceptable, but automatic restart and alerting are
  required.
- The public domain and DNS are managed through Cloudflare.

## Accepted engineering recommendations

These are the implementation baseline unless a later ADR supersedes them:

- Use a modular monolith with a single API deployment and PostgreSQL migration
  graph.
- Keep FinDB behind a provider adapter and normalized internal DTOs. Contract
  tests detect breaking upstream changes.
- Continue serving the most recent published daily report during a provider
  outage, clearly showing its `as_of` time and stale status. Do not publish a
  new report from incomplete data.
- Keep numeric chart series locale-neutral; localize labels, units, summaries,
  and narrative separately.
- Make private R2 downloads short-lived signed URLs issued only after API
  authorization. This browser-to-R2 transfer is an explicit data-plane
  exception to the single nginx ingress rule.
- Record every privileged conversation view and policy/configuration change in
  an append-only audit trail.

## Open product and compliance items

These do not block the Phase 0 foundation, but must be resolved before the
named capability is accepted:

- Whether membership has a contractual `seat_limit`, and whether exceeding it
  is blocked or merely alerted.
- Invitation/email provider, password-reset policy, and whether MFA is
  mandatory for `admin`.
- Exact asset operations for `asset_manager`, maximum upload size, supported
  MIME types, malware scanning, versioning, and deletion/recovery policy.
- Which derived indicators and report formulas exist, who approves them, and
  whether multilingual narrative is machine-generated or editorially
  reviewed.
- Final FinDB authentication, rate limits, revision semantics, time zones, and
  normalized contract; its current documentation is
  <https://findb.tingfong.com/docs>.
- DeepSeek or successor-provider data retention, training use, data location,
  and cross-border-transfer terms.
- A legal review of indefinite conversation retention under Singapore PDPA.
  The launch setting may be indefinite while still preserving an
  administrator-only legal deletion/hold mechanism. Product UI restrictions
  must not prevent the organization from satisfying a lawful access,
  correction, preservation, or disposal obligation.

## Conversation risk controls

Indefinite retention increases the impact of compromise, unnecessary
collection, model-provider transfer, and insider access. Before production:

- document collection purpose and model processing in the privacy notice;
- minimize or redact credentials and unnecessary personal/sensitive data
  before model submission;
- encrypt database, backups, and transport;
- scope admin queries by organization/user/date and audit every full-message
  view;
- separate application roles from database/operations access;
- provide internal data-access, legal-hold, and lawful-disposal procedures;
- keep retention a configurable policy even though the initial value is
  indefinite.
