# Phase 1 identity, tenancy, and market policy

Status: implemented; acceptance evidence is maintained in the API integration
tests.

## Authentication boundary

- There is no public registration.
- An admin or the bootstrap command provisions an account and receives a
  cryptographically random 12-character temporary password exactly once.
  The shared generator guarantees lowercase, uppercase, digit and symbol characters
  for customer members, internal accounts and bootstrap admins; existing passwords
  and the eight-character minimum for user-selected passwords are unchanged.
- PostgreSQL stores only a versioned scrypt hash. A separate deployment secret
  is appended as a password pepper.
- A provisioned user may authenticate, inspect their own identity, change the
  temporary password, or log out. Other authenticated capabilities remain
  blocked until the password is changed.
- Password changes revoke every existing session and issue a new opaque
  session and CSRF token.
- Session and CSRF tokens are stored only as keyed hashes. The session token is
  carried by a secure HTTP-only, same-site cookie; authenticated mutations also
  require the per-session `X-CSRF-Token`.
- Login failures use a generic response and are rate limited by normalized
  email plus the trusted client address. Attempts are held in PostgreSQL with
  expiry cleanup, so limits survive process restarts and apply across workers.
- Deployment secrets must be independent, non-placeholder values of at least
  32 characters. Login, password-change, and one-time-password responses use
  `Cache-Control: no-store`.

The initial release intentionally has no forgot-password or email flow. Admin
MFA, password recovery, and Singapore-region SES are confirmed follow-up work.

## Authorization boundary

- `admin` can manage organizations, internal users, customer memberships,
  contractual seat limits, and organization market policy.
- `asset_manager` has no organization, membership, market-policy, or audit
  administration permission. Its R2 capabilities arrive in Phase 4.
- `org_member` has no back-office permission.
- Product tenant identity comes from the authenticated active membership.
  Browser headers cannot select or override an organization.
- Suspending or archiving an organization revokes all member sessions in the
  same transaction. Inactive members cannot create new sessions, and
  reactivation never revives a previously revoked session.
- Login, password change, organization lifecycle, and membership lifecycle
  acquire PostgreSQL row locks in organization → membership → user order before
  creating or revoking sessions. This prevents a concurrent login from
  committing a session after an archive, suspension, or removal revocation.
- Login checks the trial deadline again after awaited login-attempt clearing and
  any legacy password rehash, immediately before constructing the session. This
  final synchronous check is the authorization point for issuance; the transaction
  may commit afterward. Expiry returns the generic HTTP 401 without a cookie and
  rolls back the uncommitted identity transaction, including a pending rehash;
  no new session or login audit is persisted. Login-attempt accounting and clearing
  retain their existing separate transactions and are not part of that rollback.
- Password change then locks and reloads its initiating session with
  `populate_existing=True`, so the earlier authentication dependency's ORM
  snapshot cannot hide a concurrent revocation. Before mutating credentials it
  rechecks the session's user association, revocation and expiry, plus the current
  trial deadline. A revoked or expired initiating session returns HTTP 401 without
  changing credentials, issuing a session or recording a password-change audit.
  The session row lock also serializes this check with logout; concurrent password
  changes follow the same user → session order.
- Password change checks initiating-session and trial expiry again after password
  verification/hashing, immediately before changing credentials, and after the
  awaited session-revocation update, immediately before constructing the new
  session. The final synchronous check is the authorization point for issuance;
  the transaction may commit afterward. Expiry at either check returns HTTP 401
  without a cookie, and closing the uncommitted database session rolls back even
  flushed password changes and session revocations. CSRF validation remains at
  the request boundary; a subsequent token rotation does not invalidate an
  already-validated request.

## Contractual seats

- Every organization has a positive `seat_limit`.
- Every non-removed regular membership and unexpired trial membership occupies one
  seat, including a suspended user. Expired trials retain their account and history
  but release their seat immediately, without a scheduled cleanup job.
- Renewing an expired trial or converting it to regular membership checks capacity
  under the same organization row lock as provisioning. A full organization returns
  HTTP 409; reducing the seat limit also uses the current, expiry-aware occupancy.
- Membership removal is logical: it timestamps the membership and preserves
  conversation referential integrity while releasing the seat.
- Provisioning locks the organization row before counting occupied seats.
  Concurrent requests therefore cannot allocate the same last seat.
- A renewal or addendum may change the limit, but it cannot be reduced below
  current occupancy. The contract reference, reason, actor, request ID, and
  before/after values are audited.

## Market visibility

- Missing policy means visible.
- An admin may create a deny override for any of the eight canonical markets.
- Restoring visibility removes the current override; audit history remains.
- Customer market reads filter server-side using authenticated membership.

## Audit evidence

Authentication events and privileged mutations write an `audit_events` row in
the same transaction as the business change. No application endpoint updates
or deletes audit events. Admin reads can be scoped by organization and are
bounded to 500 records per request. Organization creation, contractual seat
changes, membership removal, and market-policy updates retain the relevant
reason, contract reference, actor, request ID, and before/after evidence.

## Automated acceptance

`test_phase1_integration.py` uses PostgreSQL to verify:

- temporary-password gating, password policy, session rotation, logout
  revocation, and CSRF;
- shared endpoint login rate limiting and trusted proxy address handling;
- admin, asset-manager, and member RBAC;
- suspended-member occupancy and removal-based seat release;
- concurrent last-seat provisioning and cross-organization email conflicts;
- organization archive login blocking and permanent session revocation;
- request-ID storage boundaries and bootstrap-admin login;
- PATCH and DELETE archive paths, including permanent session revocation;
- contract limit changes and rejection below occupancy;
- default visibility, tenant-specific restrictions, ignored spoofed tenant
  headers, and audit records.

## Trial memberships (2026-10-08)

Trial is a membership category, not a new system role. Trials retain `org_member`
permissions and the organization's market policies. The admin directory supports
all, regular and trial filters; account suspension remains a separate state.

- `POST /api/admin/organizations/{organization_id}/members` accepts `member_type`
  (`regular` by default, or `trial`) and positive integer `trial_days`. Trials default
  to seven days, starting at provisioning, with each day exactly 24 hours.
  Reusing a removed regular membership in the same organization as a trial returns
  `409` before changing the account, password, membership or audit trail. Removed
  regular-to-regular, trial-to-trial and trial-to-regular reprovisioning remain
  supported, with a new temporary password, mandatory first password change and
  the existing seat checks. A new membership in another organization can be a
  trial even when the account has a removed regular membership elsewhere.
- `PATCH` on the member accepts a timezone-aware `trial_expires_at` to shorten,
  extend or renew a trial, or `member_type: regular` to convert it. Regular-to-trial
  conversion is rejected. Trial-only inputs on a regular member are rejected.
- Responses include `member_type`, `trial_expires_at` and `trial_expired`. The
  database requires a deadline for trials and a null deadline for regular members.
  Migration `20261008_0033` leaves all existing memberships regular.
- Deadlines are stored in UTC. The editor explicitly displays and interprets Taipei
  time (UTC+8), independent of the browser's timezone. An open directory refreshes
  at the next known deadline to update status and available seats.
- At `now >= trial_expires_at`, login, password change and subsequent protected API
  requests fail authentication. Trial sessions cannot outlive the deadline. Already
  started downloads or generations may finish. This exception does not allow a
  waiting password change to create credentials after its session is revoked.
- Renewing or converting an expired trial revokes existing sessions and requires
  a fresh login; shortening a deadline to the past also revokes sessions. Active
  trials keep their sessions when extended or converted, with the original session
  expiry unchanged. Every request still checks the current membership deadline.
  Editing deadlines or converting does not reactivate a suspended account.
- New memberships use `membership.created` with no prior membership snapshot.
  Reprovisioning a removed membership uses `membership.restored`, with the snapshot
  taken before modifying the account or membership. Creation, restoration and
  removal record the reason and resulting category/deadline, account status,
  joining time, removal time and removing actor; restoration and
  `membership.removed` include those fields in both before and after snapshots.
  `membership.updated` retains category/deadline before and after values. No
  account or conversation is deleted on expiry.

Acceptance is covered by trial schema, migration and identity integration tests,
including concurrent last-seat renewal versus provisioning, stale-session rejection,
first-password-change gating, suspension preservation and conversion conflicts.

### Validation evidence (2026-10-08)

- Implemented from `origin/main` at `44e1b7f` on `feat/admin-ux-trial-members`.
- `pnpm check` passed: 289 web tests, 34 API-client tests, 1,159 API tests,
  OpenAPI drift checking, formatting, lint, type checks and both production builds.
- After the final session-renewal and detail-usage adjustments, 48 targeted API
  tests and all 289 web tests passed; final type, format and lint checks passed.
- Existing browser regression: 66 passed. The four new audio layout tests passed
  after measuring document coordinates rather than viewport scroll position;
  the final five-test layout run also verified month grouping and long titles.
- Database tests used a disposable PostgreSQL 17 container with a dedicated test
  database. No existing database or remote deployment was modified.

### Reprovisioning repair validation (2026-10-08)

- Five new PostgreSQL cases passed:
  `test_removed_regular_cannot_be_reprovisioned_as_trial_without_mutation`,
  `test_removed_member_reprovisioning_preserves_credentials_seats_and_audit`
  (`regular-regular`, `trial-trial`, `trial-regular`), and
  `test_removed_regular_can_be_provisioned_as_trial_in_another_organization`.
  The rejection case compares every persisted column in users, memberships,
  sessions and audit events before and after the rejected request. The allowed
  cases verify seat enforcement, new credentials, password-change gating and
  creation/removal/restoration reasons and snapshots.
- Four existing regressions also passed:
  `test_suspended_member_occupies_seat_until_membership_removal`,
  `test_trial_login_expiry_sessions_seats_and_conversion`,
  `test_trial_renewal_and_creation_cannot_take_same_last_seat`, and
  `test_active_trial_extension_preserves_session_and_immediate_expiry_revokes`.
- The targeted integration command below passed with `9 passed, 29 deselected`.
  `DAILY_INSIGHTS_TEST_DATABASE_URL` pointed only to the dedicated disposable
  PostgreSQL 17 database. The initial sandboxed attempt could not connect to
  localhost (`Operation not permitted`); the permitted local-network rerun passed.
  Targeted Ruff lint and strict mypy checks passed for the router and integration
  test file.
- Root `pnpm format` and `pnpm format:check` passed; only the admin router,
  identity integration tests and this document changed. The disposable container
  and its test data volume were removed after validation.

```sh
UV_CACHE_DIR=/tmp/daily-insights-uv-cache uv run --project apps/api pytest -c apps/api/pyproject.toml apps/api/tests/test_phase1_integration.py -k 'removed_regular or removed_member_reprovisioning or trial_login_expiry_sessions_seats_and_conversion or trial_renewal_and_creation_cannot_take_same_last_seat or active_trial_extension_preserves_session_and_immediate_expiry_revokes or suspended_member_occupies_seat_until_membership_removal' --tb=short
```

### Password-change session revalidation (2026-10-08)

- Twelve deterministic PostgreSQL cases in
  `test_waiting_password_change_revalidates_session_before_mutation` pause an
  actual API request after authentication and immediately before its first
  lifecycle lock. Expire → renew and expire → convert reject the waiting trial
  request after the admin mutation revokes all sessions. Additional cases cover
  logout, session expiry and another password change for internal users, regular
  members and trials, plus expiry of the current trial membership while waiting.
- Every rejection returns HTTP 401 without a session cookie and leaves every
  persisted column in users, memberships, sessions and audit events unchanged
  from the completed invalidating action. Fresh login succeeds with the unchanged
  password (or the password set by the first successful concurrent change) when
  the membership remains eligible. Existing successful password changes, initial
  password-change gating, trial session caps and restoration regressions remain
  covered by the full identity integration file.
- The command below passed with `50 passed` and no skips against a separate
  disposable PostgreSQL 17 container. Targeted Ruff and strict mypy checks passed
  for the identity router and integration test file. Root `pnpm format` and
  `pnpm format:check` passed. Only the identity router, integration tests and this
  document received additional edits; the existing admin repair was preserved.
  The disposable test container was removed after validation.

```sh
UV_CACHE_DIR=/tmp/daily-insights-uv-cache uv run --project apps/api pytest -c apps/api/pyproject.toml apps/api/tests/test_phase1_integration.py --tb=short
```

### Password-change expiry during credential work (2026-10-08)

- Twelve deterministic PostgreSQL API cases in
  `test_password_change_rolls_back_expiry_during_credential_work` move the router
  clock exactly to the deadline after real password verification, real password
  hashing or the awaited SQL session-revocation update. Each phase covers
  initiating-session expiry for internal users, regular members and trials, plus
  trial-membership expiry with an otherwise valid initiating session.
- Every case returns HTTP 401 without `Set-Cookie` and preserves every persisted
  column of users, memberships, sessions and audit events. The revocation cases
  additionally verify that the changed hash, cleared first-password flag and
  revoked sessions are already visible inside the transaction before expiry;
  unchanged post-request snapshots prove that these flushed mutations roll back.
- The full integration command above passed with `62 passed` and no skips on a
  separate disposable PostgreSQL 17 container, including the previous twelve
  waiting-request races, restoration regressions, successful password changes,
  initial-password gating and trial session caps. API Ruff lint, strict mypy,
  root `pnpm format` and `pnpm format:check` passed. Only the identity router,
  integration tests and this document received additional edits; the existing
  admin repair was preserved. The disposable container was removed afterward.

```sh
UV_CACHE_DIR=/tmp/daily-insights-uv-cache uv run --project apps/api pytest -c apps/api/pyproject.toml apps/api/tests/test_phase1_integration.py -k credential_work --tb=short
UV_CACHE_DIR=/tmp/daily-insights-uv-cache pnpm lint:api
UV_CACHE_DIR=/tmp/daily-insights-uv-cache pnpm lint:check:api
UV_CACHE_DIR=/tmp/daily-insights-uv-cache pnpm type:check:api
UV_CACHE_DIR=/tmp/daily-insights-uv-cache pnpm format
UV_CACHE_DIR=/tmp/daily-insights-uv-cache pnpm format:check
```

### Login expiry during awaited work (2026-10-08)

- Two deterministic PostgreSQL API cases in
  `test_trial_login_rolls_back_expiry_during_login_work` advance the router clock
  exactly to the trial deadline after real login-attempt clearing or real legacy
  password hashing. Before the issuance guard, both returned HTTP 200; after it,
  both return generic HTTP 401 without `Set-Cookie` and preserve every persisted
  column of users, memberships, sessions and audit events. The tests do not claim
  rollback of the separately committed login-attempt counters.
- `test_valid_trial_login_rehashes_legacy_password` verifies a valid trial still
  upgrades its legacy hash, keeps the first-password-change requirement, writes
  one login audit, issues a cookie and caps its session at the trial deadline.
- The full phase-1 integration file and password-work/security tests passed with
  `76 passed` (65 integration cases and 11 password cases), with no skips, against
  a dedicated disposable PostgreSQL 17 container. API Ruff lint and strict mypy,
  root `pnpm format` and `pnpm format:check` passed. Only the identity router,
  integration tests and this document received additional edits; prior admin and
  password-change repairs were preserved. The disposable container and its data
  volume were removed after validation.

```sh
UV_CACHE_DIR=/tmp/daily-insights-uv-cache uv run --project apps/api pytest -c apps/api/pyproject.toml apps/api/tests/test_phase1_integration.py apps/api/tests/test_password_work.py apps/api/tests/test_security.py --tb=short
UV_CACHE_DIR=/tmp/daily-insights-uv-cache pnpm lint:api
UV_CACHE_DIR=/tmp/daily-insights-uv-cache pnpm lint:check:api
UV_CACHE_DIR=/tmp/daily-insights-uv-cache pnpm type:check:api
UV_CACHE_DIR=/tmp/daily-insights-uv-cache pnpm format
UV_CACHE_DIR=/tmp/daily-insights-uv-cache pnpm format:check
```
