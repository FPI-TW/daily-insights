#!/bin/sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root_dir"

for script in scripts/production/*.sh scripts/check-production-deployment-contract.sh scripts/test-production-lifecycle.sh; do
  sh -n "$script"
done
python3 scripts/test-runtime-env-materialization.py

compose_file=compose.production.yaml
nginx_file=infra/production/nginx/default.conf.template
nginx_main=infra/production/nginx/nginx.conf
unit_file=infra/systemd/daily-insights.service

if grep -Eq '^[[:space:]]*(build:|image: [^$])' "$compose_file"; then
  echo "production Compose must use only externally supplied image variables" >&2
  exit 1
fi
if grep -q 'postgres:' "$compose_file"; then
  echo "production Compose must use external RDS, not a local PostgreSQL service" >&2
  exit 1
fi

for service in api web nginx; do
  grep -q "^  ${service}:" "$compose_file"
done
grep -q 'restart: unless-stopped' "$compose_file"
grep -q 'stop_grace_period:' "$compose_file"
grep -q 'healthcheck:' "$compose_file"
grep -q 'read_only: true' "$compose_file"

grep -q 'listen 443 ssl;' "$nginx_file"
grep -q 'ssl_certificate ' "$nginx_file"
grep -q 'server_name ${PUBLIC_HOSTNAME};' "$nginx_file"
grep -q 'real_ip_header CF-Connecting-IP;' "$nginx_main"
grep -q 'include /etc/nginx/cloudflare-realip.conf;' "$nginx_main"
grep -q 'proxy_set_header X-Forwarded-For $remote_addr;' "$nginx_file"
grep -q 'proxy_set_header X-Forwarded-Proto https;' "$nginx_file"

if grep -Eq 'proxy_set_header X-(Real-IP|Forwarded-For) \\$(http_|proxy_add_)' "$nginx_file"; then
  echo "production nginx must discard untrusted client forwarded headers" >&2
  exit 1
fi
if grep -Eq 'proxy_pass .*r2|R2_(ACCESS|SECRET|ACCOUNT)' \
  infra/production/nginx/*.conf infra/production/nginx/*.template; then
  echo "nginx must not proxy R2 bytes or contain R2 credentials" >&2
  exit 1
fi

grep -q 'After=network-online.target docker.service' "$unit_file"
grep -q 'Requires=docker.service' "$unit_file"
grep -q 'Restart=on-failure' "$unit_file"
grep -q 'up --no-build --remove-orphans' "$unit_file"
grep -q 'health.sh' "$unit_file"
grep -q 'preflight.sh' "$unit_file"
grep -q 'login-registries.sh' "$unit_file"
grep -q 'materialize-runtime-env.sh' "$unit_file"

temporary_dir=$(mktemp -d)
trap 'rm -rf "$temporary_dir"' EXIT HUP INT TERM
sed -E 's/sha256:0{64}/sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/g; s/podcasts\.example\.com/podcast.example.test/' \
  infra/production/env/release.env.example >"$temporary_dir/release.env"
scripts/production/validate-release.sh "$temporary_dir/release.env"
scripts/production/render-release-manifest.sh \
  'registry.example.test/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'registry.example.test/web@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
  'docker.io/library/nginx@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' \
  'podcast.example.test' >"$temporary_dir/rendered-release.env"
scripts/production/validate-release.sh "$temporary_dir/rendered-release.env"

mkdir "$temporary_dir/registry-stubs"
cat >"$temporary_dir/registry-stubs/aws" <<'EOF'
#!/bin/sh
[ "$*" = "ecr get-login-password --region ap-southeast-1" ]
printf 'contract-ecr-token\n'
EOF
cat >"$temporary_dir/registry-stubs/docker" <<'EOF'
#!/bin/sh
token=$(cat)
[ "$token" = "contract-ecr-token" ]
printf '%s\n' "$*" >>"$REGISTRY_LOGIN_LOG"
EOF
chmod +x "$temporary_dir/registry-stubs/aws" "$temporary_dir/registry-stubs/docker"
scripts/production/render-release-manifest.sh \
  '123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  '123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/web@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
  'docker.io/library/nginx@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' \
  'podcast.example.test' >"$temporary_dir/ecr-release.env"
: >"$temporary_dir/registry-login.log"
PATH="$temporary_dir/registry-stubs:$PATH" \
  REGISTRY_LOGIN_LOG="$temporary_dir/registry-login.log" \
  scripts/production/login-registries.sh "$temporary_dir/ecr-release.env" >/dev/null
[ "$(wc -l <"$temporary_dir/registry-login.log" | tr -d ' ')" -eq 1 ]
grep -q '^login --username AWS --password-stdin 123456789012.dkr.ecr.ap-southeast-1.amazonaws.com$' \
  "$temporary_dir/registry-login.log"

sed 's/@sha256:[a-f0-9]*/:latest/' \
  "$temporary_dir/release.env" >"$temporary_dir/tagged-release.env"
if scripts/production/validate-release.sh "$temporary_dir/tagged-release.env" 2>/dev/null; then
  echo "release validation must reject mutable image tags" >&2
  exit 1
fi

mkdir "$temporary_dir/runtime" "$temporary_dir/config"
mkdir "$temporary_dir/config/tls" "$temporary_dir/stubs"
sed \
  -e 's#USER:PASSWORD@RDS_PRIVATE_HOST#daily_insights:strong-db-password@db.internal#' \
  -e 's#REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS#contract-session-secret-12345678901234567890#' \
  -e 's#REPLACE_WITH_A_DIFFERENT_32_CHARACTER_SECRET#contract-password-pepper-098765432109876543#' \
  -e 's#findb.example.com#findb.vendor.invalid#' \
  -e 's#REPLACE_FROM_SECRET_STORE#contract-secret-value#g' \
  -e 's#ACCOUNT_ID#tenant12345#' \
  infra/production/env/api.env.example >"$temporary_dir/runtime/api.env"
cp infra/production/env/web.env.example "$temporary_dir/runtime/web.env"
chmod 0600 "$temporary_dir/runtime/api.env"
chmod 0644 "$temporary_dir/runtime/web.env"
printf '%s\n' 'set_real_ip_from 104.16.0.0/13;' >"$temporary_dir/config/cloudflare-realip.conf"

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
  %u)
    echo 0
    ;;
  %a)
    case "${3:-}" in
      */api.env | */origin.key) echo 600 ;;
      *) echo 644 ;;
    esac
    ;;
  *)
    exit 1
    ;;
esac
EOF
chmod +x "$temporary_dir/stubs/stat"

sed "s#DAILY_INSIGHTS_CONFIG_DIR=/etc/daily-insights#DAILY_INSIGHTS_CONFIG_DIR=$temporary_dir/config#; s#DAILY_INSIGHTS_RUNTIME_DIR=/run/daily-insights#DAILY_INSIGHTS_RUNTIME_DIR=$temporary_dir/runtime#" \
  "$temporary_dir/release.env" >"$temporary_dir/compose.env"

docker compose \
  --project-name daily-insights-production-contract \
  --env-file "$temporary_dir/compose.env" \
  --file compose.production.yaml \
  config --quiet
docker compose \
  --project-name daily-insights-production-contract \
  --env-file "$temporary_dir/compose.env" \
  --file compose.production.yaml \
  config --format json >"$temporary_dir/compose.json"
python3 scripts/production/validate-compose-model.py "$temporary_dir/compose.json"

PATH="$temporary_dir/stubs:$PATH" \
  scripts/production/preflight.sh "$temporary_dir/compose.env" >/dev/null

cp "$temporary_dir/runtime/api.env" "$temporary_dir/runtime/api.valid"
sed 's/contract-secret-value/REPLACE_FROM_SECRET_STORE/' \
  "$temporary_dir/runtime/api.valid" >"$temporary_dir/runtime/api.env"
if PATH="$temporary_dir/stubs:$PATH" \
  scripts/production/preflight.sh "$temporary_dir/compose.env" >/dev/null 2>&1; then
  echo "preflight must reject placeholder API values" >&2
  exit 1
fi
cp "$temporary_dir/runtime/api.valid" "$temporary_dir/runtime/api.env"

printf '%s\n' 'set_real_ip_from 0.0.0.0/0;' >"$temporary_dir/config/cloudflare-realip.conf"
if PATH="$temporary_dir/stubs:$PATH" \
  scripts/production/preflight.sh "$temporary_dir/compose.env" >/dev/null 2>&1; then
  echo "preflight must reject a default-route Cloudflare allowlist" >&2
  exit 1
fi
printf '%s\n' 'set_real_ip_from 192.0.2.0/24;' >"$temporary_dir/config/cloudflare-realip.conf"
if PATH="$temporary_dir/stubs:$PATH" \
  scripts/production/preflight.sh "$temporary_dir/compose.env" >/dev/null 2>&1; then
  echo "preflight must reject documentation/test networks" >&2
  exit 1
fi
printf '%s\n' 'set_real_ip_from 104.16.0.0/13;' >"$temporary_dir/config/cloudflare-realip.conf"

openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout "$temporary_dir/config/tls/mismatch.key" \
  -out "$temporary_dir/config/tls/mismatch.crt" \
  -subj '/CN=other.example.test' \
  -addext 'subjectAltName=DNS:other.example.test' >/dev/null 2>&1
cp "$temporary_dir/config/tls/mismatch.crt" "$temporary_dir/config/tls/origin.crt"
if PATH="$temporary_dir/stubs:$PATH" \
  scripts/production/preflight.sh "$temporary_dir/compose.env" >/dev/null 2>&1; then
  echo "preflight must reject a TLS hostname mismatch" >&2
  exit 1
fi
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout "$temporary_dir/config/tls/origin.key" \
  -out "$temporary_dir/config/tls/origin.crt" \
  -subj '/CN=podcast.example.test' \
  -addext 'subjectAltName=DNS:podcast.example.test' >/dev/null 2>&1
if PATH="$temporary_dir/stubs:$PATH" \
  scripts/production/preflight.sh "$temporary_dir/compose.env" >/dev/null 2>&1; then
  echo "preflight must reject a certificate inside the minimum validity window" >&2
  exit 1
fi

./scripts/test-production-lifecycle.sh "$temporary_dir"

echo "production deployment contract is valid"
