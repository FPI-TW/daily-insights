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
  built images pinned by digest across eight containers: API, Web, nginx, the
  unified orchestration worker, the newsroom (key news) worker, CloakBrowser and
  its egress proxy, and the 08:00 Asia/Taipei dispatcher;
  PostgreSQL is deliberately absent because production uses RDS;
- [`deploy.sh`](../../scripts/production/deploy.sh),
  [`preflight.sh`](../../scripts/production/preflight.sh),
  [`health.sh`](../../scripts/production/health.sh), and
  [`diagnose.sh`](../../scripts/production/diagnose.sh) validate host TLS
  material, deploy directly supplied GitHub environment values, require health
  convergence, and report container failures;
- [`infra/production/env`](../../infra/production/env) documents the GitHub
  production Environment contract; those files are never copied to EC2;
- [`release.yml`](../../.github/workflows/release.yml) reuses a passing pull
  request CI run that verified the identical tree (otherwise it runs CI), publishes
  x86_64 API/Web images to GHCR, deploys immutable digests over SSH, and
  validates health after migration;
- [`install-host-bundle.sh`](../../scripts/production/install-host-bundle.sh)
  installs root-owned Compose, nginx, and shell deployment assets and enables
  Docker without requiring host Python or AWS CLI.

Run `make check-production-deployment` before packaging or installing these
files.

## Recommended topology

- One x86_64 EC2 application instance in a private or tightly restricted subnet
  runs `api`, `web`, `nginx`, `orchestration-worker`, `newsroom-worker`,
  `cloakbrowser`, `browser-egress`, and `orchestration-dispatcher`. The
  dispatcher persists one daily RoutineRun; the worker claims provider
  functions; the API verifies Podcast uploads synchronously; the newsroom worker
  continuously polls news sources, reads article pages through CloakBrowser, and
  runs the key-news LLM stages (it idles while `DAILY_INSIGHTS_NEWSROOM_ENABLED`,
  set from the GitHub Variable `DAILY_INSIGHTS_DAILY_NEWS_ENABLED`, is false).
  Size from
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

- Store database credentials, session/password secrets, enabled-provider API
  keys, API R2 credentials, the newsroom embedding and Slack webhook secrets,
  and the deployment SSH key in the protected GitHub `production` environment.
  Scope the API R2 key to the application bucket with object
  read/write/delete and listing permissions: signing, verification and orphan
  cleanup now share the API runtime. Retired media-worker credentials are no
  longer required by release or Compose.
- The newsroom reuses the legacy news pipeline's production settings. Its
  on/off switch is the existing Variable `DAILY_INSIGHTS_DAILY_NEWS_ENABLED` and
  its DeepSeek key the existing Secret `DAILY_INSIGHTS_NEWS_MODEL_API_KEY`;
  release passes them to the deployment as `DAILY_INSIGHTS_NEWSROOM_ENABLED` and
  `DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY`, and no GitHub setting has those names.
  The only new production setting is the Secret
  `DAILY_INSIGHTS_NEWSROOM_EMBEDDING_API_KEY`. The existing flag is already
  `true`, so create that Secret before the first deployment that includes the
  newsroom, or release validation stops the deployment.
  `DAILY_INSIGHTS_NEWSROOM_SLACK_WEBHOOK_URL` is an optional Secret.
- Only `newsroom-worker` receives the embedding key and ingestion settings; the
  orchestration worker receives the newsroom LLM key only for the 08:00 editor
  pass. Neither worker receives session secrets; the newsroom worker receives no
  R2 credentials, while the orchestration worker receives them for the heatmap
  upload described below.
- The GitHub SSH action passes Secrets only to the deployment process. Compose
  writes API values into Docker's container configuration when creating the
  API container; no application env file is written or mounted on EC2.
- Docker daemon access can reveal container environment and is equivalent to
  root access. Restrict Docker group membership and never print Compose's
  rendered environment, `docker inspect` environment, or deployment shell
  tracing in logs.
- Rotate application secrets on a documented schedule and immediately after
  suspected exposure. R2 credentials should be scoped to the required bucket
  and operations. The orchestration worker receives the same R2 variables as
  the API because it uploads heatmap PNGs; issue it a token limited to
  object read/write/delete on that one bucket, never account-wide access.

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
- `newsroom-worker` unhealthy, newsroom Slack `stage_fatal` or
  `late_fill_abandoned` notices, and no published key-news edition by 09:05
  Asia/Taipei (see the [key news operations runbook](news-recovery.md));
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
   the API, orchestration dispatcher/worker, every legacy scheduler,
   `newsroom-worker`, and `data-management-worker`, confirms they are stopped,
   verifies that the legacy management and report queues have no
   pending/running rows, and runs `alembic upgrade head`. After migration it
   starts and health-checks `orchestration-worker`, starts `browser-egress` and
   `cloakbrowser`, starts and health-checks `newsroom-worker`, then converges
   API, Web, and `orchestration-dispatcher` without recreating nginx again.
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

正式 `daily-insights` bucket 必須啟用 R2 Data Access Logs，以記錄成功的 object
read、write、list、multipart 與 delete 操作。Data Access Logs 為非同步、best-effort，
保留七天且不補記啟用前事件；事故調查仍須搭配應用程式 audit/event。Cloudflare
自 2026-12-01 起將此功能納入 Workers Observability 計價，值班人員應監看用量。
部署驗收時到 bucket Settings 確認狀態為 `Enabled`，並從 Workers Observability
以 `r2` dataset、`bucket = daily-insights` 驗證新操作可查詢。

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

Each signed upload includes the client's 64-character lowercase SHA-256.
Send `Content-Type`, `If-None-Match: *`, and `x-amz-meta-sha256` exactly as
returned. The API verifies metadata and hashes the complete streamed object
before registration. Playback signing checks HEAD without streaming the file.
Keep the bucket private; CORS does not grant object authorization.

## Direct-upload production verification

Before the first release of the synchronous upload flow, complete the
[manual Podcast cutover](podcast-upload-cutover.md). Retiring the old worker
and reconciling its sessions are one-time operator tasks, never CI/CD tasks.

Verify the API key can list/read/write/delete the application bucket and the
API's `podcast-upload-spool` volume is writable by `nobody`. It provides disk
space beyond the unchanged 64 MiB `/tmp` tmpfs. The per-file hard limit stays
256 MiB. Verification has a nine-minute application deadline; nginx waits ten
minutes on `/api/admin/podcasts/direct-uploads/complete`. These origin deadlines
do not override Cloudflare's [proxy read timeout](https://developers.cloudflare.com/fundamentals/reference/connection-limits/)
(default 125 seconds). Measure the complete response through the production
proxy, especially for boundary-size files. A proxy timeout leaves the outcome
uncertain: retry the original upload receipt to recover a committed result
instead of deleting or overwriting the object.

In a production browser, upload MP3 and MP4 files, including representative
files below 20 MB and a boundary-size file. Confirm preflight accepts the exact
origin, PUT, and signed headers. Signing creates no upload batch/session. The
complete response marks the locale complete without polling or media worker.
Verify checksum, readable duration/chapters and private playback. Test replacement
with stale/current versions, a lost PUT/complete response, refresh after PUT,
and one failed locale alongside a successful locale. Reload must allow a new
upload immediately; version conflicts still require explicit replacement.

Confirmed registration failures remove unreferenced objects. Ambiguous commits
are rechecked before deletion; unavailable DB/storage retains the object for
retry. Legacy v1/v2 tickets PUT to temporary
`podcasts/direct/{expires}/{trading-date}/{locale}/{asset-id}.{ext}`.
v2/v3 completions register `podcasts/YYYY/MM/DD/{locale}/podcast_{version}.{mp3|mp4}`
with padded dates and the locale variant version, then clean staging after commit.
Existing files and v1 completions retain their paths; v2 and v3 use the same final paths. Verify replacement preserves
archived versions and a retry returns the same asset even after staging deletion.
New v3 tickets PUT to
`podcasts/staging/{scope}/{expires}/{trading-date}/{locale}/{asset-id}.{ext}`.
The HMAC scope binds environment, database connection identity (excluding password),
R2 endpoint and bucket to the session signing secret. Every five minutes cleanup
lists only that exact scope. Both URL expiry and last modification must exceed the
default 24-hour grace; the date/object locks protect the final modification-time
and Asset-reference checks. A different catalog or environment cannot sweep this
scope even when its bucket and signing secret are identical.

Final audio is never swept based on missing local DB references. Unscoped v1/v2
staging, legacy/import paths and old session tombstones are retained for manual
reconciliation; neither the API loop nor the retired media-worker loop runs legacy
session deletion. Explicit upload compensation still cleans its own attempt, and
explicit episode removal retains its existing behavior. Malformed paths are skipped.
Final collisions require matching size/MIME/full SHA and no DB references; otherwise
409 leaves the destination untouched. Unknown commit/write outcomes retain objects.
Failed scoped-staging deletions and late scoped uploads are retried by later sweeps.
Successful deletion emits `podcast.object.deleted`; background staging cleanup also
emits `podcast.staging_cleanup.deleted` with environment, bucket and object key.
Neither event includes credentials or signed URLs.

Use separate buckets and bucket-scoped R2 tokens for development, staging and
production. Never copy production R2 write/delete credentials into local tests.
The scope is a safeguard for accidental shared-bucket configuration, not an access
control boundary. A clone with identical environment, DB identity and session secret
is indistinguishable; change the environment/DB and credentials before starting it.
Session-secret, DB identity or endpoint changes retain the previous scope for manual
review; DB password rotation preserves it. Final/unscoped orphan retention can grow
storage usage, so reconcile a reviewed key list across all owning catalogs before
manual deletion. No new cloud service is required.

Deploy this fix to every API replica and any legacy worker that can access the
bucket, and stop older binaries before resuming background work. Existing v1/v2
tickets remain accepted; new v3 tickets require the fixed API and are incompatible
with a rollback to the old API. Verify new upload/retry/playback and check the scoped
cleanup events before any audio recovery. Code changes alone do not protect objects
from an old process or an external token with delete access.

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

For scoped background cleanup and deployment/rollback precautions, see the
[Podcast cleanup isolation runbook](podcast-upload-cutover.md#2026-10-23-清理隔離修正部署).

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
