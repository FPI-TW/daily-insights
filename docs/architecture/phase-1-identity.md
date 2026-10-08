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
  started downloads or generations may finish.
- Renewing or converting an expired trial revokes existing sessions and requires
  a fresh login; shortening a deadline to the past also revokes sessions. Active
  trials keep their sessions when extended or converted, with the original session
  expiry unchanged. Every request still checks the current membership deadline.
  Editing deadlines or converting does not reactivate a suspended account.
- Creation and updates retain category/deadline before and after values in the
  existing audit trail. No account or conversation is deleted on expiry.

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
