# Phase 1 identity, tenancy, and market policy

Status: implemented; acceptance evidence is maintained in the API integration
tests.

## Authentication boundary

- There is no public registration.
- An admin or the bootstrap command provisions an account and receives a
  cryptographically random temporary password exactly once.
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
- Every non-removed membership occupies one seat, including a suspended user.
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
