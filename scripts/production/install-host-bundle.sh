#!/bin/sh
set -eu

source_root=${1:-}
install_root=${DAILY_INSIGHTS_INSTALL_ROOT:-/opt/daily-insights}
state_dir=${DAILY_INSIGHTS_STATE_DIR:-/var/lib/daily-insights}
config_dir=${DAILY_INSIGHTS_CONFIG_DIR:-/etc/daily-insights}

if [ "$(id -u)" -ne 0 ]; then
  echo "host bundle installation must run as root" >&2
  exit 1
fi
if [ -z "$source_root" ] || [ ! -d "$source_root" ]; then
  echo "usage: $0 RELEASE_BUNDLE_ROOT" >&2
  exit 2
fi

for command in aws curl docker openssl python3 systemctl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "required host command is missing: $command" >&2
    exit 1
  fi
done
docker compose version >/dev/null

install -d -o root -g root -m 0755 \
  "$install_root" \
  "$install_root/infra/production/nginx" \
  "$install_root/scripts/production" \
  "$state_dir" \
  "$config_dir" \
  "$config_dir/tls"

install -o root -g root -m 0644 \
  "$source_root/compose.production.yaml" \
  "$install_root/compose.production.yaml"
for file in nginx.conf default.conf.template; do
  install -o root -g root -m 0644 \
    "$source_root/infra/production/nginx/$file" \
    "$install_root/infra/production/nginx/$file"
done
for file in \
  common.sh \
  deploy.sh \
  health.sh \
  login-registries.sh \
  materialize-runtime-env.py \
  materialize-runtime-env.sh \
  preflight.sh \
  render-release-manifest.sh \
  rollback.sh \
  update-cloudflare-realip.sh \
  validate-certificate-hostname.py \
  validate-cloudflare-allowlist.py \
  validate-compose-model.py \
  validate-release.sh; do
  install -o root -g root -m 0755 \
    "$source_root/scripts/production/$file" \
    "$install_root/scripts/production/$file"
done

install -o root -g root -m 0644 \
  "$source_root/infra/production/env/ssm.env.example" \
  "$config_dir/ssm.env.example"
install -o root -g root -m 0644 \
  "$source_root/infra/systemd/daily-insights.service" \
  /etc/systemd/system/daily-insights.service

systemctl daemon-reload
systemctl enable daily-insights.service >/dev/null
echo "host bundle installed; configure SSM, TLS, Cloudflare, and current.env before starting"
