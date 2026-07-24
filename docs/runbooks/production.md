# Production runbook recommendation

Target: AWS Singapore (`ap-southeast-1`), fewer than 1,000 concurrent users,
daily data freshness, and a product that accepts brief outages but requires
automatic recovery and alerting.

This is a recommendation and readiness checklist. It does not deploy anything.

## Recommended topology

- One EC2 application instance in a private or tightly restricted subnet runs
  nginx, web, and API containers. Start with a current general-purpose Graviton
  instance only after confirming all images are multi-architecture; otherwise
  use x86_64. Size from measured SSE memory and CPU, not user count alone.
- RDS PostgreSQL in private subnets is the durable store. A Single-AZ instance
  is compatible with accepted downtime and lower cost; Multi-AZ is the
  recommended upgrade if recovery time becomes stricter.
- Cloudflare manages public DNS, edge TLS/WAF, and proxies only to nginx.
- Cloudflare R2 remains the asset store. Authorized private downloads use
  short-lived signed R2 URLs and therefore bypass the EC2/nginx data path.
- No legacy database or R2 namespace is reused. Existing systems remain
  untouched.

Lightsail is not the production baseline because RDS networking, IAM,
CloudWatch, security-group control, and future scaling are clearer with EC2.

## Network and TLS

1. Put RDS in private subnets with no public address. Its security group accepts
   PostgreSQL only from the application security group.
2. Expose only nginx HTTP/HTTPS on EC2. Restrict SSH in favor of AWS Systems
   Manager Session Manager; if SSH is unavoidable, restrict it to named
   administrator CIDRs.
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
- A systemd unit owns the production Compose project:
  `After=network-online.target docker.service`,
  `Requires=docker.service`, and `Restart=on-failure`.
- Containers use `restart: unless-stopped`, bounded health checks, and graceful
  shutdown periods. systemd runs `docker compose up -d --remove-orphans` on
  boot and fails deployment when health does not converge.
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
  model-provider key, and R2 credentials in AWS Secrets Manager or SSM
  Parameter Store with KMS encryption.
- Grant the EC2 instance role read access only to named application secrets.
- Materialize secrets at runtime in memory or a root-readable ephemeral file;
  never bake them into images, Compose files, logs, metrics, or user-visible
  error responses.
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

1. Validate migrations and images in staging with sanitized/synthetic data.
2. Record current image digests, database migration, configuration version, and
   global model configuration.
3. Run pre-deploy database backup checks.
4. Pull pinned images, run compatible migrations once, and start containers
   through systemd/Compose.
5. Require readiness from database, API, web, and nginx before switching
   traffic.
6. Smoke-test authentication, tenant/market isolation, one report in each
   locale, SSE chat, admin authorization, and a private asset download.
7. Roll back to the prior compatible image on application failure. Database
   restore is an incident action, not a routine code rollback.

Use Cloudflare's proxied DNS with a conservative TTL during initial cutover.
Cutover must not modify or delete the legacy services or data.

## Capacity and recovery exercises

- Load-test realistic report reads plus long-lived SSE connections. The
  acceptance target is the agreed sub-1,000 concurrency with explicit CPU,
  memory, database, connection, and latency headroom.
- Test FinDB unavailability: last published data remains visible and stale,
  new incomplete publication is blocked, and an alert fires.
- Test model timeout, client disconnect, process restart, EC2 reboot, disk
  pressure, expired R2 URL, and RDS fail/restore procedures.
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
- confirmation that no legacy data was migrated or deleted.
