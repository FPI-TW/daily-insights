# Production runbook recommendation

Target: AWS Singapore (`ap-southeast-1`), fewer than 1,000 concurrent users,
daily data freshness, and a product that accepts brief outages but requires
automatic recovery and alerting.

This is a recommendation and readiness checklist. It does not deploy anything.

For the release gate, first historical backfill, validation, and operational
response for report-page index data, follow the
[index data operations runbook](index-data-operations.md).

The repository now includes an offline-verifiable deployment foundation and an
[EC2 first-deploy procedure](ec2-first-deploy.md). It does not create AWS,
Cloudflare, RDS, or R2 resources:

- [`compose.production.yaml`](../../compose.production.yaml) runs only externally
  built images pinned by digest across six containers: API, Web, nginx, the
  unified orchestration worker, the Podcast media worker, and the 08:00
  Asia/Taipei dispatcher;
  PostgreSQL is deliberately absent because production uses RDS;
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
  runs `api`, `web`, `nginx`, `orchestration-worker`, `podcast-media-worker`,
  and `orchestration-dispatcher`. The dispatcher persists one daily RoutineRun;
  workers claim provider functions and Podcast verification sessions. Size from
  measured upload spool, SSE memory, and CPU, not user count alone.
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
  API signing R2 credentials, media-worker-only R2 credentials, and the
  deployment SSH key in the protected GitHub `production` environment. Set
  `DAILY_INSIGHTS_R2_MEDIA_WORKER_ACCESS_KEY_ID` and
  `DAILY_INSIGHTS_R2_MEDIA_WORKER_SECRET_ACCESS_KEY` to a separate R2 key scoped
  to the application bucket and the media worker's `GetObject` and
  `DeleteObject` needs. The API signer keeps its own R2 credentials.
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
- Migrations are normally forward-compatible with the serving application
  during rollout. Migration `20260909_0022` is an explicit worker/scheduler
  compatibility fence, so those old processes are quiesced before it runs.
  Destructive cleanup is a later, separately approved change.
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
4. For the one-time orchestration cutover, deploy only after 10:00 Asia/Taipei
   and set `DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE` to the next Taipei
   date. For later releases, preserve that activation date; the deploy script
   detects the installed orchestration schema and permits normal deployment
   hours. Let
   the protected GitHub CD workflow send Secrets, Variables, and image
   digests to the SSH process and run
   `/opt/daily-insights/scripts/production/deploy.sh`. The script validates and
   pulls pinned images, renders and tests the nginx template in a disposable
   container, then recreates only nginx with Docker DNS re-resolution enabled
   while the previous API/Web containers are still available. It then stops
   the API, orchestration dispatcher/worker, every legacy scheduler, and
   `podcast-media-worker`, `data-management-worker`, confirms they are stopped, verifies that the
   legacy management and report queues have no pending/running rows, and runs
   `alembic upgrade head`. After migration it starts and
   health-checks `orchestration-worker` and `podcast-media-worker`, then
   converges API, Web, and `orchestration-dispatcher` without recreating nginx
   again.
   A migrated installation with no RoutineRun is activation-pending, so a
   failed first deployment can be retried before 10:00 with activation set to
   today or the next Taipei date. Once a RoutineRun exists, normal deployments
   require the preserved activation date not to be in the future.
5. Require container health plus active API-readiness and Web-login probes
   through nginx before reporting deployment success. Replaced upstream
   addresses may take up to two seconds to re-resolve; a deployment remains
   pending during that bounded transition and fails at the health deadline.
6. For the Podcast pilot, smoke-test authentication, tenant isolation, all
   three locales, Podcast publication, admin authorization, and private audio
   playback, including locale fallback and browser-local progress restoration.
   Add report and SSE chat smoke tests only when those surfaces enter the
   deployed release.

## R2 browser upload CORS

The Podcast browser PUT goes directly to the private R2 bucket. Configure an
exact allowed origin for the deployed web site; do not use `*` or a hostname
wildcard. Replace `https://podcast.example.com` below with the exact
`https://$PUBLIC_HOSTNAME` origin. For a local browser run, add its exact origin
as a separate origin only in the development bucket configuration.

```json
{
  "CORSRules": [
    {
      "AllowedOrigins": ["https://podcast.example.com"],
      "AllowedMethods": ["PUT", "GET", "HEAD"],
      "AllowedHeaders": [
        "Content-Type",
        "If-None-Match",
        "x-amz-meta-sha256",
        "Range"
      ],
      "ExposeHeaders": [
        "ETag",
        "Accept-Ranges",
        "Content-Length",
        "Content-Range"
      ],
      "MaxAgeSeconds": 3600
    }
  ]
}
```

Each batch init file includes the client's 64-character lowercase SHA-256.
The upload request must send the signed `Content-Type`, `If-None-Match: *`, and
`x-amz-meta-sha256` headers exactly as returned by batch init. The API verifies
the SHA metadata at finalize; the worker independently hashes the streamed bytes
and verifies the metadata again before cutover. Playback signing checks the R2
HEAD metadata and does not stream the whole audio object when the checksum is
present. The browser reads the `ETag` response header for transfer diagnostics.
Keep the R2 bucket private; CORS does not grant object authorization.

## Direct-upload production verification

After deploy, confirm `daily-insights-podcast-media-worker` is healthy and its
logs show its heartbeat without printing environment values. In the production
browser, upload representative MP3 and MP4 files, including a file near the
256 MiB limit. Verify the browser preflight has the exact production origin and
allows `PUT`, `Content-Type`, `If-None-Match`, and `x-amz-meta-sha256`; the PUT must target the
returned UUID object key and return an exposed `ETag`.

Finalize each locale and poll the batch status until each file is completed or
has an actionable failure. Confirm completed files have a server-computed
SHA-256, duration/chapters when readable, and appear in private playback. Test a
replacement with stale and current locale versions, a lost PUT response followed
by idempotent finalize, and a two-locale batch with one locale failure. A
completed locale remains active when another locale in the batch fails. Confirm
the worker key is distinct from the API signer key in the protected GitHub
environment and has only the required bucket permissions. Transient object-store
service or network errors stay visible as `processing` while the durable lease
backs off from 15 seconds; after five attempts the locale becomes `failed`.
The worker continues with other queued locales during a retry delay.
Orphan cleanup includes version-conflict sessions after the upload grace period;
R2 cleanup failures release the cleanup claim and defer another attempt for five
minutes. An object with an Asset row remains protected.

7. For routine recovery after this schema-boundary migration, keep every legacy
   scheduler and worker quiesced, apply a forward fix, and rerun `deploy.sh`.
   A prior image may be redeployed only when it is schema-compatible with the
   current database; never run the legacy execution model after migration 0028.

The host does not save rollback env files because they would duplicate GitHub
Secrets. If migration, replacement-worker startup or health, final convergence,
or final health fails, deployment re-confirms all legacy schedulers and the old
worker are quiescent. The operator uses emitted container state and recent logs
to correct the failure, then reruns `deploy.sh`; the deployment does not
downgrade or roll back the migration.

Migration `20260909_0022` is a rollback fence. A true rollback across that fence
is an incident procedure that restores a compatible pre-migration database
snapshot together with the prior images. Do not run the migration downgrade
against production data: it explicitly refuses once automatic scheduling
history exists.

nginx uses Docker's embedded DNS resolver for the `api` and `web` Compose
aliases. Do not replace the targeted backend convergence with an unscoped
`docker compose up`: the deployment deliberately validates and recreates nginx
first, then leaves that proxy running while backend container addresses change.

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
