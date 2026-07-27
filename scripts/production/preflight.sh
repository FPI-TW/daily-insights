#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
public_hostname=${1:-}
config_dir=${DAILY_INSIGHTS_CONFIG_ROOT:-/etc/daily-insights}
certificate_file="$config_dir/tls/origin.crt"
private_key_file="$config_dir/tls/origin.key"
allowlist_file="$config_dir/cloudflare-realip.conf"

if [ -z "$public_hostname" ]; then
  echo "usage: $0 PUBLIC_HOSTNAME" >&2
  exit 2
fi
if ! printf '%s\n' "$public_hostname" |
  grep -Eq '^[a-z0-9]([a-z0-9.-]*[a-z0-9])$' ||
  ! printf '%s\n' "$public_hostname" | grep -q '[.]'; then
  echo "PUBLIC_HOSTNAME must be a lowercase DNS hostname" >&2
  exit 1
fi

file_mode() {
  target=$1
  stat -c '%a' "$target" 2>/dev/null || stat -f '%Lp' "$target"
}

file_owner_uid() {
  target=$1
  stat -c '%u' "$target" 2>/dev/null || stat -f '%u' "$target"
}

require_regular_root_file() {
  target=$1
  label=$2
  if [ ! -f "$target" ] || [ -L "$target" ]; then
    echo "$label must be a regular non-symlink file: $target" >&2
    exit 1
  fi
  if [ "$(file_owner_uid "$target")" != "0" ]; then
    echo "$label must be owned by root: $target" >&2
    exit 1
  fi
}

require_regular_root_file "$allowlist_file" "Cloudflare real-IP allowlist"
"$script_dir/validate-cloudflare-allowlist-host.sh" "$allowlist_file"

require_regular_root_file "$certificate_file" "Origin certificate"
require_regular_root_file "$private_key_file" "Origin private key"
if [ "$(file_mode "$private_key_file")" != "600" ]; then
  echo "Origin private key must have mode 0600" >&2
  exit 1
fi

minimum_validity_seconds=${DAILY_INSIGHTS_TLS_MIN_VALIDITY_SECONDS:-604800}
case "$minimum_validity_seconds" in
  '' | *[!0-9]*)
    echo "DAILY_INSIGHTS_TLS_MIN_VALIDITY_SECONDS must be an integer" >&2
    exit 2
    ;;
esac
if ! openssl x509 -in "$certificate_file" -noout -checkhost "$public_hostname" |
  grep -Fqx "Hostname $public_hostname does match certificate"; then
  echo "origin certificate does not match $public_hostname" >&2
  exit 1
fi
openssl x509 -in "$certificate_file" -noout -checkend "$minimum_validity_seconds" >/dev/null
openssl pkey -in "$private_key_file" -noout >/dev/null

temporary_dir=$(mktemp -d)
trap 'rm -rf "$temporary_dir"' EXIT HUP INT TERM
openssl x509 -in "$certificate_file" -pubkey -noout |
  openssl pkey -pubin -outform DER >"$temporary_dir/cert.der"
openssl pkey -in "$private_key_file" -pubout -outform DER >"$temporary_dir/key.der"
if ! cmp -s "$temporary_dir/cert.der" "$temporary_dir/key.der"; then
  echo "origin certificate and private key do not match" >&2
  exit 1
fi

echo "production host preflight passed"
