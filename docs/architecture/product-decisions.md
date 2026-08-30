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
- Every organization has a positive contractual `seat_limit`. It is a hard
  upper bound, not a display-only member count. Creating a membership at the
  limit is rejected atomically.
- Suspended users continue to occupy a seat. A seat is released only when the
  membership is removed; temporarily suspending an account cannot be used to
  provision beyond the contract limit.
- An internal `admin` may change `seat_limit` after a renewal or contract
  addendum. The change records actor, reason, contract reference, timestamp,
  and before/after values in the audit trail.
- The internal back office has two privileged roles:
  - `admin`: organization and member CRUD, organization market visibility,
    shared asset management, model configuration, and conversation viewing.
  - `asset_manager`: shared R2 asset maintenance only.
- `org_member` is a customer-facing member with no back-office permissions.

### Identity provisioning

- Public self-registration is not allowed.
- An internal `admin` creates member accounts. The system generates a
  cryptographically random temporary password; only its hash is persisted and
  the plaintext is displayed once to the creating admin for delivery through
  the team's existing secure out-of-band channel. It cannot be retrieved
  later.
- A provisioned account is active but marked as requiring a password change.
  The user must replace the temporary password after the first successful
  login before accessing other product capabilities.
- The initial release does not send account email.
- The initial release does not provide a forgot-password or self-service
  password-reset flow. This is confirmed follow-up work and does not block the
  initial release.
- Admin MFA is a confirmed post-initial-release security requirement. Email
  delivery for that future flow uses Amazon SES in Singapore
  (`ap-southeast-1`). Neither MFA nor SES blocks the initial release.

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

### First-wave morning-report plan

The long-term catalog remains eight markets, but the current first-wave launch
is fixed to macro/bonds, crypto, and US equities. Taiwan equities and Taiwan
index derivatives retain their existing UI code and direct-URL mock previews,
but are absent from report navigation, the report list, the launch manifest,
and production publications. FX, Hong Kong, and Mainland China also remain
outside this wave.

Twelve Data is the temporary sole raw-data provider for the three launch
markets. There is no runtime fallback to FinDB or another provider, no proxy
substitution, and no cross-provider history stitching. Only complete blocks
that pass the licensed read-only probe enter the versioned manifest; unsupported
blocks have no production UI slot. The detailed source gate and acceptance
checklist live in
[`twelve-data-three-market-morning-report-plan.md`](twelve-data-three-market-morning-report-plan.md).
The previous
[`five-market-morning-report-baseline.md`](five-market-morning-report-baseline.md)
is retained only as historical investigation evidence.

### Reports, data, and localization

- Primary reports are rendered from fetched structured data and charts, not
  presented as PDF documents.
- PDF remains an allowed downloadable asset format.
- Reports, chart labels, and narrative content support Traditional Chinese
  (`zh-hant`), Simplified Chinese (`zh-hans`), and English (`en`).
- Twelve Data is the temporary only raw-data source for the three-market
  first-wave contract. Its license, exact endpoints/symbols, history, credits,
  freshness, and external-display rights require a credentialed read-only
  probe. FinDB remains isolated behind its adapter for future work and is not a
  runtime fallback for this launch.
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
- After the complete application architecture is established, Podcast is the
  first customer-facing vertical slice.
- Formal eight-market content and the customer report interface remain pending
  during the Podcast pilot.
- Every authenticated organization shares one Podcast catalog; market
  visibility policy does not filter Podcast episodes.
- Initial Podcast audio already in R2 is migrated to immutable,
  backend-generated, locale-aware keys that include the asset ID. Migration is
  a copy/verify/cutover workflow. Inventory assigns each source object an
  explicitly reviewed locale rather than inferring it from a legacy key, and
  registration uses that verified locale. The active mapping changes only
  after complete size/MIME/checksum reconciliation, the application never
  automatically deletes source objects, and internal staff manually remove
  old-path copies only after verified cutover.
- The privileged browser upload endpoint accepts one to three files per
  request, with at most one file for each supported locale. It writes to the
  stable canonical key
  `podcasts/{trading-date}/audio/{locale}/podcast.{mp3|mp4}`. Confirmed
  replacement with the same extension overwrites that locale's same stable key.
  A replacement that changes between MP3 and MP4 writes the new
  stable-extension key before deleting the replaced old-format key. Both paths
  increment the logical version.
- Podcast metadata supports all three locales and audio may vary by locale.
  Publishing requires at least one active locale; no particular locale is
  mandatory. Playback first selects an exact requested-locale match and then
  falls back in the fixed order `zh-hant` → `zh-hans` → `en`.
- The customer surface uses the native HTML audio element, retains playback
  progress in browser localStorage, and provides no product download or
  offline-listening workflow.
- Podcast uses one admin-specified unique `trading_date` and has no
  show/series, season, episode number, or scheduled publication in the pilot.
- A duplicate `trading_date + locale` media operation warns before logical
  replacement and requires the expected current version. Confirmed replacement
  follows the stable-key rules above, updates checksum and object metadata,
  increments the logical version, and is audited.
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
- Keep every raw-data provider, including Twelve Data and FinDB, behind a
  provider adapter and normalized internal DTOs. Contract tests detect breaking
  upstream changes.
- The current foundation implementation is fail-closed: it serves the most
  recent published daily report during a provider outage and does not publish
  from incomplete data. This is an implementation note, not the accepted
  future morning-report contract; the three-market plan supersedes it with
  `complete`/`partial`/`unavailable` publication states and a publication for
  every edition, including failed source-run evidence.
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
