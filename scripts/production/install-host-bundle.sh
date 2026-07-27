#!/bin/sh
set -eu

source_root=${1:-}
install_root=${DAILY_INSIGHTS_INSTALL_ROOT:-/opt/daily-insights}
config_dir=/etc/daily-insights

if [ "$(id -u)" -ne 0 ]; then
  echo "host bundle installation must run as root" >&2
  exit 1
fi
if [ -z "$source_root" ] || [ ! -d "$source_root" ]; then
  echo "usage: $0 RELEASE_BUNDLE_ROOT" >&2
  exit 2
fi

for command in docker openssl systemctl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "required host command is missing: $command" >&2
    exit 1
  fi
done
docker compose version >/dev/null
systemctl enable --now docker.service >/dev/null

install -d -o root -g root -m 0755 \
  "$install_root" \
  "$install_root/infra/production/nginx" \
  "$install_root/scripts/production" \
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
  deploy.sh \
  diagnose.sh \
  health.sh \
  preflight.sh \
  validate-cloudflare-allowlist-host.sh; do
  install -o root -g root -m 0755 \
    "$source_root/scripts/production/$file" \
    "$install_root/scripts/production/$file"
done

echo "host bundle installed; Docker owns reboot recovery through container restart policies"
