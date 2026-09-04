#!/bin/sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root_dir"

for script in \
  scripts/production/*.sh \
  scripts/check-production-deployment-contract.sh \
  scripts/test-production-nginx-dns.sh; do
  sh -n "$script"
done

compose_file=compose.production.yaml
nginx_file=infra/production/nginx/default.conf.template
nginx_main=infra/production/nginx/nginx.conf
ci_workflow_file=.github/workflows/ci.yml
workflow_file=.github/workflows/release.yml

if grep -Eq '^[[:space:]]*build:' "$compose_file"; then
  echo "production Compose must not build images on the host" >&2
  exit 1
fi
if grep -Eq 'env_file:|DAILY_INSIGHTS_(CONFIG|RUNTIME)_DIR' "$compose_file"; then
  echo "production Compose must receive GitHub deployment values directly, not host env files" >&2
  exit 1
fi
grep -Fq 'image: ${API_IMAGE:?set API_IMAGE to an immutable digest reference}' "$compose_file"
grep -Fq 'image: ${WEB_IMAGE:?set WEB_IMAGE to an immutable digest reference}' "$compose_file"
nginx_image=$(awk 'index($0, "image: docker.io/library/nginx@sha256:") { print $2 }' "$compose_file")
if ! printf '%s\n' "$nginx_image" |
  grep -Eq '^docker[.]io/library/nginx@sha256:[a-f0-9]{64}$' ||
  printf '%s\n' "$nginx_image" | grep -Eq '@sha256:0{64}$'; then
  echo "production nginx image must be pinned in Compose by immutable digest" >&2
  exit 1
fi
if grep -q 'postgres:' "$compose_file"; then
  echo "production Compose must use external RDS, not a local PostgreSQL service" >&2
  exit 1
fi

production_services="api web nginx morning-report-scheduler daily-news-scheduler analyst-viewpoints-scheduler index-daily-bars-scheduler"
for service in $production_services; do
  grep -q "^  ${service}:" "$compose_file"
  grep -q "container_name: daily-insights-${service}" "$compose_file"
done
# Derived from the list above rather than hardcoded: every service must declare
# the restart policy, and adding one should not need this number edited too.
[ "$(grep -c 'restart: unless-stopped' "$compose_file")" -eq "$(printf '%s\n' $production_services | wc -l | tr -d ' ')" ]
grep -q 'stop_grace_period:' "$compose_file"
grep -q 'healthcheck:' "$compose_file"
grep -Fq "st_mtime < 93600" "$compose_file"
grep -q 'read_only: true' "$compose_file"

for name in \
  DAILY_INSIGHTS_DATABASE_URL \
  DAILY_INSIGHTS_SESSION_SECRET \
  DAILY_INSIGHTS_PASSWORD_PEPPER \
  DAILY_INSIGHTS_R2_ENDPOINT_URL \
  DAILY_INSIGHTS_R2_BUCKET_NAME \
  DAILY_INSIGHTS_R2_ACCESS_KEY_ID \
  DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY \
  DAILY_INSIGHTS_R2_SIGNED_URL_TTL_SECONDS; do
  grep -Fq "${name}: \${${name}:?" "$compose_file"
done
grep -Fq 'DAILY_INSIGHTS_TWELVE_DATA_BASE_URL: ${DAILY_INSIGHTS_TWELVE_DATA_BASE_URL:-https://api.twelvedata.com}' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_TWELVE_DATA_API_KEY: ${DAILY_INSIGHTS_TWELVE_DATA_API_KEY:-}' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_DAILY_NEWS_ENABLED: ${DAILY_INSIGHTS_DAILY_NEWS_ENABLED:-false}' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_MODEL_API_KEY: ${DAILY_INSIGHTS_MODEL_API_KEY:-}' "$compose_file"
grep -Fq 'daily_insights_api.scripts.run_daily_news' "$compose_file"
grep -Fq '/tmp/daily-news-heartbeat' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED: ${DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED:-false}' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_ANALYST_VIEWPOINTS_BASE_URL: ${DAILY_INSIGHTS_ANALYST_VIEWPOINTS_BASE_URL:-https://analyst-viewpoints.invalid}' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY: ${DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY:-}' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS: ${DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS:-10}' "$compose_file"
grep -Fq 'daily_insights_api.scripts.run_analyst_viewpoints' "$compose_file"
grep -Fq '/tmp/analyst-viewpoints-heartbeat' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_YFINANCE_ENABLED: ${DAILY_INSIGHTS_YFINANCE_ENABLED:-false}' "$compose_file"
grep -Fq 'daily_insights_api.scripts.run_index_daily_bars' "$compose_file"
grep -Fq '/tmp/index-daily-bars-heartbeat' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_CHAT_ENABLED: ${DAILY_INSIGHTS_CHAT_ENABLED:-false}' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_CHAT_MODEL_PROVIDER: ${DAILY_INSIGHTS_CHAT_MODEL_PROVIDER:-deepseek}' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_CHAT_MODEL_NAME: ${DAILY_INSIGHTS_CHAT_MODEL_NAME:-deepseek-chat}' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_CHAT_MODEL_API_BASE_URL: ${DAILY_INSIGHTS_CHAT_MODEL_API_BASE_URL:-https://api.deepseek.com}' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_CHAT_MODEL_API_KEY: ${DAILY_INSIGHTS_CHAT_MODEL_API_KEY:-}' "$compose_file"
grep -Fq 'DAILY_INSIGHTS_CHAT_TIMEOUT_SECONDS: ${DAILY_INSIGHTS_CHAT_TIMEOUT_SECONDS:-90}' "$compose_file"

grep -Fq '/etc/daily-insights/cloudflare-realip.conf:/etc/nginx/cloudflare-realip.conf:ro' "$compose_file"
grep -Fq '/etc/daily-insights/tls/origin.crt:/etc/nginx/tls/origin.crt:ro' "$compose_file"
grep -Fq '/etc/daily-insights/tls/origin.key:/etc/nginx/tls/origin.key:ro' "$compose_file"
grep -q 'listen 443 ssl;' "$nginx_file"
grep -q 'ssl_certificate ' "$nginx_file"
grep -q 'server_name ${PUBLIC_HOSTNAME};' "$nginx_file"
grep -q 'real_ip_header CF-Connecting-IP;' "$nginx_main"
grep -q 'include /etc/nginx/cloudflare-realip.conf;' "$nginx_main"
grep -Fq 'resolver 127.0.0.11 valid=2s ipv6=off;' "$nginx_main"
grep -Fq 'resolver_timeout 1s;' "$nginx_main"
grep -Fq 'zone api_upstream 64k;' "$nginx_file"
grep -Fq 'server api:8000 resolve;' "$nginx_file"
grep -Fq 'zone web_upstream 64k;' "$nginx_file"
grep -Fq 'server web:3000 resolve;' "$nginx_file"
grep -Fq 'listen 127.0.0.1:8080;' "$nginx_file"
grep -Fq 'location = /nginx-health/api {' "$nginx_file"
grep -Fq 'location = /nginx-health/web {' "$nginx_file"
grep -Fq 'proxy_pass http://web_upstream/zh-hant/login;' "$nginx_file"
grep -q 'proxy_set_header X-Forwarded-For $remote_addr;' "$nginx_file"
grep -q 'proxy_set_header X-Forwarded-Proto https;' "$nginx_file"
grep -Fq 'http://127.0.0.1:8080/nginx-health/api' "$compose_file"
grep -Fq 'http://127.0.0.1:8080/nginx-health/web' "$compose_file"
grep -Fq -- '--subnet "10.253.${subnet_octet}.0/24"' scripts/test-production-nginx-dns.sh

if grep -Eq 'proxy_set_header X-(Real-IP|Forwarded-For) \\$(http_|proxy_add_)' "$nginx_file"; then
  echo "production nginx must discard untrusted client forwarded headers" >&2
  exit 1
fi
if grep -Eq 'proxy_pass .*r2|R2_(ACCESS|SECRET|ACCOUNT)' \
  infra/production/nginx/*.conf infra/production/nginx/*.template; then
  echo "nginx must not proxy R2 bytes or contain R2 credentials" >&2
  exit 1
fi

grep -q 'systemctl enable --now docker.service' scripts/production/install-host-bundle.sh
if awk '
  /^on:$/ { in_triggers = 1; next }
  in_triggers && /^[^[:space:]#]/ { exit }
  in_triggers && /^[[:space:]]+push:/ { found = 1 }
  END { exit found ? 0 : 1 }
' "$ci_workflow_file"; then
  echo "CI must not run directly on main pushes because release runs it before deployment" >&2
  exit 1
fi
[ "$(grep -Fc 'uses: ./.github/workflows/ci.yml' "$workflow_file")" -eq 1 ]
if grep -R -Eq 'daily-insights[.]service|/etc/daily-insights/runtime|/var/lib/daily-insights|--env-file' \
  "$workflow_file" compose.production.yaml scripts/production; then
  echo "deployment must rely on Docker restart policies without host runtime env or app systemd" >&2
  exit 1
fi
grep -Fq 'envs: GITHUB_TOKEN,GITHUB_ACTOR,API_IMAGE,WEB_IMAGE,PUBLIC_HOSTNAME,' "$workflow_file"
# The deploy validation is an inline bash script; a merge that drops a `fi`
# only surfaces as a syntax error at deploy time unless it is parsed here.
validation_script=$(mktemp)
awk '
  /name: Validate deployment configuration/ { capture = 1; next }
  capture && /^      - name: / { exit }
  capture && /^          / { sub(/^          /, ""); print }
' "$workflow_file" >"$validation_script"
if ! bash -n "$validation_script"; then
  echo "release.yml deploy validation script does not parse" >&2
  rm -f "$validation_script"
  exit 1
fi
rm -f "$validation_script"
for name in \
  DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED \
  DAILY_INSIGHTS_ANALYST_VIEWPOINTS_BASE_URL \
  DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS \
  DAILY_INSIGHTS_YFINANCE_ENABLED \
  DAILY_INSIGHTS_CHAT_ENABLED \
  DAILY_INSIGHTS_CHAT_MODEL_PROVIDER \
  DAILY_INSIGHTS_CHAT_MODEL_NAME \
  DAILY_INSIGHTS_CHAT_MODEL_API_BASE_URL \
  DAILY_INSIGHTS_CHAT_TIMEOUT_SECONDS; do
  grep -Fq "${name}: \${{ vars.${name} }}" "$workflow_file"
  grep -Fq ",${name}" "$workflow_file"
done
grep -Fq 'DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY: ${{ secrets.DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY }}' "$workflow_file"
grep -Fq ',DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY' "$workflow_file"
grep -Fq 'DAILY_INSIGHTS_CHAT_MODEL_API_KEY: ${{ secrets.DAILY_INSIGHTS_CHAT_MODEL_API_KEY }}' "$workflow_file"
grep -Fq ',DAILY_INSIGHTS_CHAT_MODEL_API_KEY' "$workflow_file"
grep -Fq '/opt/daily-insights/scripts/production/deploy.sh' "$workflow_file"

for obsolete in \
  infra/systemd/daily-insights.service \
  scripts/production/common.sh \
  scripts/production/render-release-manifest.sh \
  scripts/production/rollback.sh \
  scripts/production/validate-release.sh; do
  if [ -e "$obsolete" ]; then
    echo "obsolete persistent-env lifecycle file remains: $obsolete" >&2
    exit 1
  fi
done

temporary_dir=$(mktemp -d)
trap 'rm -rf "$temporary_dir"' EXIT HUP INT TERM
mkdir -p "$temporary_dir/config/tls" "$temporary_dir/stubs"
printf '%s\n' \
  'set_real_ip_from 104.16.0.0/13;' \
  'set_real_ip_from 2400:cb00::/32;' \
  >"$temporary_dir/config/cloudflare-realip.conf"
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout "$temporary_dir/config/tls/origin.key" \
  -out "$temporary_dir/config/tls/origin.crt" \
  -subj '/CN=podcast.example.test' \
  -addext 'subjectAltName=DNS:podcast.example.test' >/dev/null 2>&1
chmod 0600 "$temporary_dir/config/tls/origin.key"
chmod 0644 "$temporary_dir/config/tls/origin.crt"

cat >"$temporary_dir/stubs/stat" <<'EOF'
#!/bin/sh
case "${2:-}" in
  %u) echo 0 ;;
  %a)
    case "${3:-}" in
      */origin.key) echo 600 ;;
      *) echo 644 ;;
    esac
    ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$temporary_dir/stubs/stat"

PATH="$temporary_dir/stubs:$PATH" \
  DAILY_INSIGHTS_CONFIG_ROOT="$temporary_dir/config" \
  scripts/production/preflight.sh podcast.example.test >/dev/null

printf '%s\n' 'set_real_ip_from 0.0.0.0/0;' >"$temporary_dir/config/cloudflare-realip.conf"
if PATH="$temporary_dir/stubs:$PATH" \
  DAILY_INSIGHTS_CONFIG_ROOT="$temporary_dir/config" \
  scripts/production/preflight.sh podcast.example.test >/dev/null 2>&1; then
  echo "preflight must reject a default-route Cloudflare allowlist" >&2
  exit 1
fi
printf '%s\n' \
  'set_real_ip_from 104.16.0.0/13;' \
  'set_real_ip_from 2400:cb00::/32;' \
  >"$temporary_dir/config/cloudflare-realip.conf"
if PATH="$temporary_dir/stubs:$PATH" \
  DAILY_INSIGHTS_CONFIG_ROOT="$temporary_dir/config" \
  scripts/production/preflight.sh other.example.test >/dev/null 2>&1; then
  echo "preflight must reject a TLS hostname mismatch" >&2
  exit 1
fi

export API_IMAGE=registry.example.test/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export WEB_IMAGE=registry.example.test/web@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
export PUBLIC_HOSTNAME=podcast.example.test
export DAILY_INSIGHTS_DATABASE_URL=postgresql+psycopg://daily_insights:test@db.internal/daily_insights
export DAILY_INSIGHTS_SESSION_SECRET=contract-session-secret-12345678901234567890
export DAILY_INSIGHTS_PASSWORD_PEPPER=contract-password-pepper-098765432109876543
export DAILY_INSIGHTS_MORNING_REPORTS_ENABLED=false
export DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED=false
export DAILY_INSIGHTS_ANALYST_VIEWPOINTS_BASE_URL=
export DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY=
export DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS=10
export DAILY_INSIGHTS_CHAT_ENABLED=true
export DAILY_INSIGHTS_CHAT_MODEL_PROVIDER=deepseek
export DAILY_INSIGHTS_CHAT_MODEL_NAME=deepseek-chat
export DAILY_INSIGHTS_CHAT_MODEL_API_BASE_URL=https://api.deepseek.com
export DAILY_INSIGHTS_CHAT_MODEL_API_KEY=contract-chat-model-key
export DAILY_INSIGHTS_CHAT_TIMEOUT_SECONDS=90
export DAILY_INSIGHTS_TWELVE_DATA_BASE_URL=
export DAILY_INSIGHTS_TWELVE_DATA_API_KEY=
export DAILY_INSIGHTS_DAILY_NEWS_ENABLED=false
export DAILY_INSIGHTS_MODEL_API_KEY=
export DAILY_INSIGHTS_R2_ENDPOINT_URL=https://tenant.r2.cloudflarestorage.com
export DAILY_INSIGHTS_R2_BUCKET_NAME=production-podcast-assets
export DAILY_INSIGHTS_R2_ACCESS_KEY_ID=contract-r2-access
export DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY=contract-r2-secret
export DAILY_INSIGHTS_R2_SIGNED_URL_TTL_SECONDS=900

docker compose \
  --project-name daily-insights-production-contract \
  --file compose.production.yaml \
  config --quiet
docker compose \
  --project-name daily-insights-production-contract \
  --file compose.production.yaml \
  config --format json >"$temporary_dir/compose.json"
python3 scripts/production/validate-compose-model.py "$temporary_dir/compose.json"
scripts/test-production-nginx-dns.sh "$nginx_image"

cat >"$temporary_dir/stubs/docker" <<'EOF'
#!/bin/sh
echo "docker $*" >>"$DEPLOYMENT_LOG"
if [ "${1:-}" = "inspect" ]; then
  echo "${DOCKER_INSPECT_STATE:-running healthy}"
fi
exit 0
EOF
cat >"$temporary_dir/stubs/sudo" <<'EOF'
#!/bin/sh
echo "sudo $*" >>"$DEPLOYMENT_LOG"
exit 0
EOF
cat >"$temporary_dir/stubs/timeout" <<'EOF'
#!/bin/sh
shift
exec "$@"
EOF
chmod +x "$temporary_dir/stubs/docker" "$temporary_dir/stubs/sudo" "$temporary_dir/stubs/timeout"
: >"$temporary_dir/deployment.log"
PATH="$temporary_dir/stubs:$PATH" \
  DEPLOYMENT_LOG="$temporary_dir/deployment.log" \
  scripts/production/deploy.sh >/dev/null
grep -q 'compose .* config --quiet' "$temporary_dir/deployment.log"
grep -q 'compose .* pull' "$temporary_dir/deployment.log"
grep -q 'compose .* run --rm --no-deps nginx nginx -t' "$temporary_dir/deployment.log"
grep -q 'compose .* up -d --no-build --force-recreate --no-deps nginx' "$temporary_dir/deployment.log"
grep -q 'compose .* run --rm --no-deps api alembic upgrade head' "$temporary_dir/deployment.log"
grep -q 'compose .* up -d --no-build --remove-orphans api web morning-report-scheduler daily-news-scheduler analyst-viewpoints-scheduler index-daily-bars-scheduler' "$temporary_dir/deployment.log"
grep -q 'exec daily-insights-nginx wget -q -T 2 -O /dev/null http://127.0.0.1:8080/nginx-health/api' "$temporary_dir/deployment.log"
grep -q 'exec daily-insights-nginx wget -q -T 2 -O /dev/null http://127.0.0.1:8080/nginx-health/web' "$temporary_dir/deployment.log"

nginx_validate_line=$(grep -n 'run --rm --no-deps nginx nginx -t' "$temporary_dir/deployment.log" | cut -d: -f1)
nginx_recreate_line=$(grep -n 'up -d --no-build --force-recreate --no-deps nginx' "$temporary_dir/deployment.log" | cut -d: -f1)
migration_line=$(grep -n 'run --rm --no-deps api alembic upgrade head' "$temporary_dir/deployment.log" | cut -d: -f1)
backend_converge_line=$(grep -n 'up -d --no-build --remove-orphans api web morning-report-scheduler daily-news-scheduler analyst-viewpoints-scheduler index-daily-bars-scheduler' "$temporary_dir/deployment.log" | cut -d: -f1)
if [ "$nginx_validate_line" -ge "$nginx_recreate_line" ] ||
  [ "$nginx_recreate_line" -ge "$migration_line" ] ||
  [ "$migration_line" -ge "$backend_converge_line" ]; then
  echo "deployment must validate/recreate nginx before migrating and replacing backends" >&2
  exit 1
fi
if grep -Eq -- '--env-file|systemctl|daily-insights[.]service' "$temporary_dir/deployment.log"; then
  echo "deployment unexpectedly used a host env file or app systemd unit" >&2
  exit 1
fi

if PATH="$temporary_dir/stubs:$PATH" \
  DEPLOYMENT_LOG="$temporary_dir/deployment.log" \
  DAILY_INSIGHTS_MORNING_REPORTS_ENABLED=true \
  scripts/production/deploy.sh >/dev/null 2>&1; then
  echo "enabled morning reports must require Twelve Data launch configuration" >&2
  exit 1
fi

if PATH="$temporary_dir/stubs:$PATH" \
  DEPLOYMENT_LOG="$temporary_dir/deployment.log" \
  DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED=true \
  scripts/production/deploy.sh >/dev/null 2>&1; then
  echo "enabled analyst viewpoints must require upstream launch configuration" >&2
  exit 1
fi

if PATH="$temporary_dir/stubs:$PATH" \
  DEPLOYMENT_LOG="$temporary_dir/deployment.log" \
  DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED=true \
  DAILY_INSIGHTS_ANALYST_VIEWPOINTS_BASE_URL=https://analyst.example.test \
  DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY=contract-analyst-key \
  DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS=0 \
  scripts/production/deploy.sh >/dev/null 2>&1; then
  echo "enabled analyst viewpoints must require a valid upstream timeout" >&2
  exit 1
fi

if PATH="$temporary_dir/stubs:$PATH" \
  DEPLOYMENT_LOG="$temporary_dir/deployment.log" \
  DAILY_INSIGHTS_DAILY_NEWS_ENABLED=true \
  scripts/production/deploy.sh >/dev/null 2>&1; then
  echo "enabled daily news must require a model API key" >&2
  exit 1
fi

if PATH="$temporary_dir/stubs:$PATH" \
  DEPLOYMENT_LOG="$temporary_dir/deployment.log" \
  DOCKER_INSPECT_STATE='restarting unhealthy' \
  DAILY_INSIGHTS_HEALTH_TIMEOUT_SECONDS=240 \
  scripts/production/health.sh >"$temporary_dir/health.out" 2>"$temporary_dir/health.err"; then
  echo "production health must fail immediately for a restarting container" >&2
  exit 1
fi
grep -q 'entered terminal runtime state: restarting' "$temporary_dir/health.err"

echo "production deployment contract is valid"
