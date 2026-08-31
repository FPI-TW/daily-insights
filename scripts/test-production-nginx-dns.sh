#!/bin/sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root_dir"

nginx_image=${1:-$(awk 'index($0, "image: docker.io/library/nginx@sha256:") { print $2 }' compose.production.yaml)}
case "$nginx_image" in
  docker.io/library/nginx@sha256:????????????????????????????????????????????????????????????????) ;;
  *)
    echo "production nginx DNS test requires the pinned Compose image" >&2
    exit 2
    ;;
esac

test_suffix="$$"
network_name="daily-insights-nginx-dns-${test_suffix}"
proxy_name="daily-insights-nginx-dns-proxy-${test_suffix}"
api_name="daily-insights-nginx-dns-api-${test_suffix}"
web_name="daily-insights-nginx-dns-web-${test_suffix}"
holder_name="daily-insights-nginx-dns-holder-${test_suffix}"
temporary_dir=$(mktemp -d)

cleanup() {
  docker rm -f "$proxy_name" "$api_name" "$web_name" "$holder_name" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
  rm -rf "$temporary_dir"
}
trap cleanup EXIT HUP INT TERM

cat >"$temporary_dir/cloudflare-realip.conf" <<'EOF'
set_real_ip_from 127.0.0.1/32;
EOF

openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout "$temporary_dir/origin.key" \
  -out "$temporary_dir/origin.crt" \
  -subj '/CN=daily-insights.test' \
  -addext 'subjectAltName=DNS:daily-insights.test' >/dev/null 2>&1

write_mock_config() {
  target=$1
  port=$2
  health_status=$3
  body=$4
  cat >"$target" <<EOF
events {}
http {
  server {
    listen ${port};
    location = /health/ready { return ${health_status} "${body}-ready\n"; }
    location / { return 200 "${body}\n"; }
  }
}
EOF
}

write_mock_config "$temporary_dir/api-v1.conf" 8000 200 api-v1
write_mock_config "$temporary_dir/api-v2.conf" 8000 200 api-v2
write_mock_config "$temporary_dir/api-503.conf" 8000 503 api-unavailable
write_web_config() {
  target=$1
  body=$2
  cat >"$target" <<EOF
events {}
http {
  server {
    listen 3000;
    location = / { return 200 "${body}\n"; }
    location = /zh-hant/login { return 200 "${body}\n"; }
  }
}
EOF
}

write_web_config "$temporary_dir/web-v1.conf" web-v1
write_web_config "$temporary_dir/web-v2.conf" web-v2
cat >"$temporary_dir/web-error.conf" <<'EOF'
events {}
http { server { listen 3000; location / { return 503 "web-unavailable\n"; } } }
EOF
cat >"$temporary_dir/api-stall.conf" <<'EOF'
events {}
http {
  server {
    listen 8000;
    location / { proxy_connect_timeout 10s; proxy_read_timeout 10s; proxy_pass http://192.0.2.1:81; }
  }
}
EOF

network_created=false
subnet_octet=$(( ($$ % 200) + 20 ))
network_attempt=0
while [ "$network_attempt" -lt 20 ]; do
  if docker network create \
    --subnet "10.253.${subnet_octet}.0/24" \
    "$network_name" >/dev/null 2>&1; then
    network_created=true
    break
  fi
  subnet_octet=$(( (subnet_octet % 220) + 20 ))
  network_attempt=$((network_attempt + 1))
done
if [ "$network_created" != true ]; then
  echo "unable to allocate an isolated Docker test subnet" >&2
  exit 1
fi

start_mock() {
  name=$1
  alias_name=$2
  config_file=$3
  docker run --rm -d \
    --name "$name" \
    --network "$network_name" \
    --network-alias "$alias_name" \
    --mount "type=bind,src=${config_file},dst=/etc/nginx/mock.conf,readonly" \
    "$nginx_image" \
    nginx -c /etc/nginx/mock.conf -g 'daemon off;' >/dev/null
}

start_mock "$api_name" api "$temporary_dir/api-v1.conf"
start_mock "$web_name" web "$temporary_dir/web-v1.conf"

docker run --rm \
  --network "$network_name" \
  --env PUBLIC_HOSTNAME=daily-insights.test \
  --mount "type=bind,src=${root_dir}/infra/production/nginx/nginx.conf,dst=/etc/nginx/nginx.conf,readonly" \
  --mount "type=bind,src=${root_dir}/infra/production/nginx/default.conf.template,dst=/etc/nginx/templates/default.conf.template,readonly" \
  --mount "type=bind,src=${temporary_dir}/cloudflare-realip.conf,dst=/etc/nginx/cloudflare-realip.conf,readonly" \
  --mount "type=bind,src=${temporary_dir}/origin.crt,dst=/etc/nginx/tls/origin.crt,readonly" \
  --mount "type=bind,src=${temporary_dir}/origin.key,dst=/etc/nginx/tls/origin.key,readonly" \
  --tmpfs /etc/nginx/conf.d:size=1m \
  "$nginx_image" nginx -T >"$temporary_dir/rendered-nginx.conf" 2>&1
grep -Fq 'resolver 127.0.0.11 valid=2s ipv6=off;' "$temporary_dir/rendered-nginx.conf"
grep -Fq 'server api:8000 resolve;' "$temporary_dir/rendered-nginx.conf"
grep -Fq 'server web:3000 resolve;' "$temporary_dir/rendered-nginx.conf"

docker run --rm -d \
  --name "$proxy_name" \
  --network "$network_name" \
  --env PUBLIC_HOSTNAME=daily-insights.test \
  --mount "type=bind,src=${root_dir}/infra/production/nginx/nginx.conf,dst=/etc/nginx/nginx.conf,readonly" \
  --mount "type=bind,src=${root_dir}/infra/production/nginx/default.conf.template,dst=/etc/nginx/templates/default.conf.template,readonly" \
  --mount "type=bind,src=${temporary_dir}/cloudflare-realip.conf,dst=/etc/nginx/cloudflare-realip.conf,readonly" \
  --mount "type=bind,src=${temporary_dir}/origin.crt,dst=/etc/nginx/tls/origin.crt,readonly" \
  --mount "type=bind,src=${temporary_dir}/origin.key,dst=/etc/nginx/tls/origin.key,readonly" \
  --tmpfs /etc/nginx/conf.d:size=1m \
  --tmpfs /var/cache/nginx:size=32m \
  --tmpfs /var/run:size=1m \
  "$nginx_image" >/dev/null

proxy_id=$(docker inspect --format '{{.Id}}' "$proxy_name")
proxy_pid=$(docker exec "$proxy_name" cat /var/run/nginx.pid)

internal_probe() {
  path=$1
  docker exec "$proxy_name" wget -q -T 2 -O /dev/null "http://127.0.0.1:8080${path}"
}

public_get() {
  path=$1
  docker exec "$proxy_name" wget -q -T 2 --no-check-certificate \
    --header 'Host: daily-insights.test' -O - "https://127.0.0.1${path}"
}

poll_body() {
  path=$1
  expected=$2
  deadline=$3
  while [ "$(date +%s)" -le "$deadline" ]; do
    if [ "$(public_get "$path" 2>/dev/null || true)" = "$expected" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

poll_body /api/test api-v1 $(( $(date +%s) + 5 ))
poll_body / web-v1 $(( $(date +%s) + 5 ))
internal_probe /nginx-health/api
internal_probe /nginx-health/web

replace_mock() {
  name=$1
  alias_name=$2
  config_file=$3
  public_path=$4
  expected_body=$5

  old_ip=$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$name")
  docker rm -f "$name" >/dev/null
  docker run --rm -d --name "$holder_name" --network "$network_name" --ip "$old_ip" \
    "$nginx_image" sleep 30 >/dev/null

  failure_observed=false
  if ! public_get "$public_path" >/dev/null 2>&1; then
    failure_observed=true
  fi

  started_at=$(date +%s)
  start_mock "$name" "$alias_name" "$config_file"
  new_ip=$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$name")
  if [ "$old_ip" = "$new_ip" ]; then
    echo "$alias_name replacement unexpectedly reused $old_ip" >&2
    exit 1
  fi
  if [ "$failure_observed" != true ]; then
    echo "$alias_name replacement did not exercise an unavailable old address" >&2
    exit 1
  fi
  poll_body "$public_path" "$expected_body" $(( started_at + 5 ))
  docker rm -f "$holder_name" >/dev/null
}

replace_mock "$api_name" api "$temporary_dir/api-v2.conf" /api/test api-v2
replace_mock "$web_name" web "$temporary_dir/web-v2.conf" / web-v2

[ "$(docker inspect --format '{{.Id}}' "$proxy_name")" = "$proxy_id" ]
[ "$(docker exec "$proxy_name" cat /var/run/nginx.pid)" = "$proxy_pid" ]

docker rm -f "$api_name" >/dev/null
start_mock "$api_name" api "$temporary_dir/api-503.conf"
sleep 3
if internal_probe /nginx-health/api >/dev/null 2>&1; then
  echo "API health must reject provider 503" >&2
  exit 1
fi

docker rm -f "$web_name" >/dev/null
start_mock "$web_name" web "$temporary_dir/web-error.conf"
sleep 3
if internal_probe /nginx-health/web >/dev/null 2>&1; then
  echo "Web health must reject non-2xx responses" >&2
  exit 1
fi

docker rm -f "$api_name" >/dev/null
start_mock "$api_name" api "$temporary_dir/api-stall.conf"
sleep 3
probe_started=$(date +%s)
if docker exec "$proxy_name" \
  wget -q -T 2 -O /dev/null http://127.0.0.1:8080/nginx-health/api \
  >/dev/null 2>&1; then
  echo "API health must reject a stalled upstream" >&2
  exit 1
fi
probe_elapsed=$(( $(date +%s) - probe_started ))
if [ "$probe_elapsed" -gt 3 ]; then
  echo "stalled upstream probe exceeded its bound" >&2
  exit 1
fi

echo "production nginx DNS replacement regression passed"
