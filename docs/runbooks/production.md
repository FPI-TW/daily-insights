# Production runbook recommendation

Target: AWS Singapore (`ap-southeast-1`), fewer than 1,000 concurrent users,
daily data freshness, and a product that accepts brief outages but requires
automatic recovery and alerting.

This is a recommendation and readiness checklist. It does not deploy anything.

The repository now includes an offline-verifiable deployment foundation and an
[EC2 first-deploy procedure](ec2-first-deploy.md). It does not create AWS,
Cloudflare, RDS, or R2 resources:

- [`compose.production.yaml`](../../compose.production.yaml) runs only externally
  built API, Web, and nginx images pinned by digest; PostgreSQL is deliberately
  absent because production uses RDS;
- [`deploy.sh`](../../scripts/production/deploy.sh),
  [`preflight.sh`](../../scripts/production/preflight.sh),
  [`health.sh`](../../scripts/production/health.sh), and
  [`diagnose.sh`](../../scripts/production/diagnose.sh) validate host TLS
  material, deploy directly supplied GitHub environment values, require health
  convergence, and report container failures;
- [`infra/production/env`](../../infra/production/env) documents the GitHub
  production Environment contract; those files are never copied to EC2;
- [`release.yml`](../../.github/workflows/release.yml) reuses CI, publishes
  x86_64 API/Web images to GHCR, deploys immutable digests over SSH, and
  validates health after migration;
- [`install-host-bundle.sh`](../../scripts/production/install-host-bundle.sh)
  installs root-owned Compose, nginx, and shell deployment assets and enables
  Docker without requiring host Python or AWS CLI.

Run `make check-production-deployment` before packaging or installing these
files.

## Recommended topology

- One x86_64 EC2 application instance in a private or tightly restricted subnet
  runs nginx, web, and API containers. Size from measured SSE memory and CPU,
  not user count alone.
- RDS PostgreSQL in private subnets is the durable store. A Single-AZ instance
  is compatible with accepted downtime and lower cost; Multi-AZ is the
  recommended upgrade if recovery time becomes stricter.
- Cloudflare manages public DNS, edge TLS/WAF, and proxies only to nginx.
- Cloudflare R2 remains the asset store. Authorized private downloads use
  short-lived signed R2 URLs and therefore bypass the EC2/nginx data path.
- No legacy database is reused. Approved existing Podcast objects in R2 are
  copied to a dedicated application prefix, verified, and cut over without
  modifying the source objects. Internal staff manually remove old-path objects
  only after migration reconciliation and playback checks; unrelated objects
  remain untouched.

Lightsail is not the production baseline because RDS networking, IAM,
CloudWatch, security-group control, and future scaling are clearer with EC2.

## Network and TLS

1. Put RDS in private subnets with no public address. Its security group accepts
   PostgreSQL only from the application security group.
2. Expose only nginx HTTPS on EC2. Restrict SSH to named administrator CIDRs.
3. Use Cloudflare Full (strict) TLS and an origin certificate or publicly
   trusted certificate on nginx. Do not use Flexible TLS.
4. Restrict the origin to Cloudflare proxy IP ranges or use Cloudflare
   authenticated origin pulls/tunnel according to the chosen network design.
5. Enable HSTS at the edge only after HTTPS and subdomain ownership are
   verified. Development nginx intentionally does not emit HSTS.
6. Preserve and validate the real client address only from trusted Cloudflare
   proxies; never trust arbitrary forwarded headers at the application edge.

## Process and restart behavior

- Build immutable, version-tagged web/API images in CI and deploy pinned image
  digests. Do not build production images on the instance.
- Containers use `restart: unless-stopped`, bounded health checks, and graceful
  shutdown periods. systemd enables and starts Docker; after an EC2 reboot,
  Docker recreates each process from the existing container configuration,
  including the environment captured by `docker compose up`.
- API shutdown stops new requests, lets active SSE/provider operations finish
  within a bounded grace period, and records interrupted generations as
  `partial` or `error`.
- Use an EC2 Auto Recovery/Status Check action or a one-instance Auto Scaling
  Group (`min=1`, `desired=1`) for host failure. Store no irreplaceable state on
  the instance.
- Apply OS and Docker security updates on a scheduled maintenance cadence and
  reboot with an explicit health/rollback check.

## Secrets

- Store production database credentials, session/password secrets, FinDB key,
  R2 credentials, and the deployment SSH key in the protected GitHub
  `production` environment.
- The GitHub SSH action passes Secrets only to the deployment process. Compose
  writes API values into Docker's container configuration when creating the
  API container; no application env file is written or mounted on EC2.
- Docker daemon access can reveal container environment and is equivalent to
  root access. Restrict Docker group membership and never print Compose's
  rendered environment, `docker inspect` environment, or deployment shell
  tracing in logs.
- Rotate application secrets on a documented schedule and immediately after
  suspected exposure. R2 credentials should be scoped to the required bucket
  and operations.

## RDS backup and restore

- Enable automated backups and point-in-time recovery. Start with at least
  14-day retention and confirm the legal/business requirement before
  production.
- Enable storage encryption, deletion protection, and final snapshots.
- Take a manual snapshot before migrations with material data-shape risk.
- Migrations are forward-compatible with the currently deployed application
  during rollout; destructive cleanup is a later, separately approved change.
- At least quarterly, restore a backup to an isolated RDS instance, run
  integrity/application smoke checks, record achieved recovery point and
  recovery time, then destroy the test resource through the approved process.
- Backups inherit conversation-retention sensitivity. Access and lifecycle
  must match the retention/legal-hold policy.

## Observability and alarms

Send structured container logs and application metrics to CloudWatch. Configure
alarm destinations before go-live and test delivery.

Minimum alarms:

- EC2 status-check failure and Auto Recovery/ASG replacement;
- filesystem usage, memory pressure (CloudWatch Agent), sustained CPU, and
  container/process unhealthy;
- nginx 5xx rate, API error rate, p95/p99 latency, and active/abnormally closed
  SSE connections;
- RDS CPU, free storage, freeable memory, connections, replica/failover events
  when applicable, and backup failures;
- daily ingestion/publication job missing its deadline, source freshness older
  than the daily SLO, FinDB contract failure, and provider error-rate spike;
- model-provider failure/latency spike and generations stuck in `pending`;
- unusual admin conversation views, repeated authentication failures, and R2
  signing/management failures.

The alert route needs an owned on-call destination, severity, acknowledgement,
and escalation timing. A dashboard without notification is not an alarm.

## Deployment and rollback

### Host contract

Provision the following before the first deployment:

1. Install the repository release bundle at `/opt/daily-insights`, owned by
   root and not writable by the application account.
2. Enable and start `docker.service`. Do not install a separate Daily Insights
   systemd unit; Docker restart policies own reboot recovery.
3. Configure the protected GitHub `production` Environment. The SSH action
   passes its values directly to Compose; do not install application env files
   on EC2.
4. Install the Cloudflare Origin CA certificate and key as
   `/etc/daily-insights/tls/origin.crt` and `origin.key`, with the private key
   root-owned mode `0600`.
5. Materialize `/etc/daily-insights/cloudflare-realip.conf` from Cloudflare's
   current published IPv4 and IPv6 ranges. Reject empty results, default routes
   (`0.0.0.0/0`, `::/0`), and stale cached data. The committed example contains
   documentation-only addresses and is never a production allowlist. Runtime
   preflight validates directive syntax and rejects unsafe/private/test ranges;
   it cannot prove that a public CIDR belongs to Cloudflare or that the list is
   current. Provisioning must verify ownership and freshness against
   Cloudflare's published source.
6. Confirm the EC2 security group enforces that origin port 443 is reachable
   only through the approved Cloudflare path; Compose publishes no API or Web
   port.

Before Compose starts, preflight verifies root ownership and restrictive file
modes, Cloudflare allowlist syntax, and the Origin CA certificate hostname,
minimum remaining validity, private-key readability, and certificate/key match.
The workflow validates the GitHub environment contract, while the API validates
its production settings again during startup. The application network is
pinned to `172.30.0.0/24`; the Uvicorn forwarded-header allowlist and
`DAILY_INSIGHTS_TRUSTED_PROXY_CIDRS` match that exact CIDR so nginx is the only
trusted application proxy.

CI obtains immutable API/Web digests from GHCR and passes them to the SSH
deployment process. The official nginx image is pinned by digest in
`compose.production.yaml`, matching FindB's choice to manage the proxy image in
Compose while retaining immutable deployment inputs.

### Release procedure

1. Validate migrations and images in staging with sanitized/synthetic data.
2. Record current image digests, database migration, configuration version, and
   application configuration.
3. Run pre-deploy database backup checks.
4. Let the protected GitHub CD workflow send Secrets, Variables, and image
   digests to the SSH process and run
   `/opt/daily-insights/scripts/production/deploy.sh`. The script validates and
   pulls pinned images, runs compatible migrations once, executes
   `docker compose up`, and waits for API/Web/nginx container health.
5. Require readiness from database, API, web, and nginx before switching
   traffic.
6. For the Podcast pilot, smoke-test authentication, tenant isolation, all
   three locales, Podcast publication, admin authorization, and private audio
   playback, including locale fallback and browser-local progress restoration.
   Add report and SSE chat smoke tests only when those surfaces enter the
   deployed release.
7. Roll back by rerunning the protected workflow for the prior compatible
   commit/image digest with the same GitHub Environment values. Database restore
   is an incident action, not a routine code rollback.

The host does not save rollback env files because they would duplicate GitHub
Secrets. A failed deployment emits container state and recent logs. Migrations
must remain backward-compatible with the previous image because application
rollback never reverses or restores the database.

Use Cloudflare's proxied DNS with a conservative TTL during initial cutover.
Cutover must not modify or delete the legacy services or data.

## Podcast R2 migration

1. Export and review a source inventory. Attach one trading date and locale to
   each source key; reject duplicates or ambiguous mappings.
2. Run migration tooling in dry-run mode and review every generated canonical
   target key.
3. Copy objects through an atomic conditional target create without deleting
   or overwriting source keys. An existing target with a different checksum is
   a hard failure. The controlled tool streams source bytes through the
   migration host because R2/S3 server-side CopyObject has no destination
   precondition; capacity planning must include host download/upload bandwidth,
   temporary memory/disk limits, timeouts, and worker-thread headroom. The
   current adapter keeps at most 8 MiB per copy in memory, spills larger bodies
   to the host temporary directory, and rejects an individual object above
   1 GiB; review these safeguards against the approved inventory before
   cutover.
4. Verify source/target size, MIME type, and SHA-256; do not treat ETag alone as
   a content checksum.
5. Reconcile manifest count, total bytes, checksums, and episode/locale
   coverage. Exercise signed playback from the canonical target paths.
6. Require explicit admin cutover before canonical assets become active.
7. Export the verified old-path removal manifest. Internal staff manually
   remove only those source keys, then record post-cleanup HEAD results. The
   application and migration tool never perform this deletion.

Until step 6, retrying migration must be idempotent and customer playback must
continue using the previously active state. A partial copy or failed
reconciliation is not a successful migration.

## Capacity and recovery exercises

- For the Podcast pilot, load-test catalog/detail reads and signed-media URL
  issuance. Add realistic report reads and long-lived SSE connections when
  those surfaces enter the deployed release. Every release records explicit
  CPU, memory, database, connection, and latency headroom against the agreed
  sub-1,000 concurrency.
- For the Podcast pilot, test process restart, EC2 reboot, disk pressure,
  missing/quarantined R2 objects, expired signed URLs, and RDS fail/restore
  procedures.
- Add FinDB outage/publication-freshness exercises when reports are deployed,
  and model timeout/client-disconnect/SSE cleanup exercises when chat is
  deployed.
- Define measured RTO/RPO after the first restore and host-recovery exercises;
  “downtime accepted” is not a substitute for observed recovery time.

## Go-live evidence

- infrastructure diagram and inventory;
- approved security groups, IAM policies, secret inventory, and rotation owners;
- successful backup restore report;
- tested alarm delivery and named responders;
- capacity report for web/API/SSE/database;
- deployment and rollback transcript;
- PDPA retention and model-provider cross-border review;
- confirmation that no legacy database data was migrated or deleted, canonical
  Podcast copies passed reconciliation, and old-path cleanup matched the
  approved removal manifest without touching unrelated objects.
