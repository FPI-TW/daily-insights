#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/common.sh"

release_file=${1:-$production_current_release}
"$script_dir/validate-release.sh" "$release_file"

config_dir=$(release_value "$release_file" DAILY_INSIGHTS_CONFIG_DIR)
runtime_dir=$(release_value "$release_file" DAILY_INSIGHTS_RUNTIME_DIR)
public_hostname=$(release_value "$release_file" PUBLIC_HOSTNAME)

api_env="$runtime_dir/api.env"
web_env="$runtime_dir/web.env"
certificate_file="$config_dir/tls/origin.crt"
private_key_file="$config_dir/tls/origin.key"
allowlist_file="$config_dir/cloudflare-realip.conf"

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

env_value() {
  env_file=$1
  env_key=$2
  value=$(sed -n "s/^${env_key}=//p" "$env_file")
  count=$(sed -n "/^${env_key}=/p" "$env_file" | wc -l | tr -d ' ')
  if [ "$count" -ne 1 ] || [ -z "$value" ]; then
    echo "$env_file must define $env_key exactly once" >&2
    exit 1
  fi
  printf '%s\n' "$value"
}

reject_placeholder() {
  label=$1
  value=$2
  if printf '%s\n' "$value" |
    grep -Eqi 'change[_-]?me|replace[_-]?|example(\.|_)|user:password|account[_-]?id|rds[_-]?private[_-]?host'; then
    echo "$label contains a placeholder value" >&2
    exit 1
  fi
}

require_regular_root_file "$api_env" "API runtime env"
if [ "$(file_mode "$api_env")" != "600" ]; then
  echo "API runtime env must have mode 0600" >&2
  exit 1
fi

api_required_keys="
DAILY_INSIGHTS_ENVIRONMENT
DAILY_INSIGHTS_DATABASE_URL
DAILY_INSIGHTS_SESSION_SECRET
DAILY_INSIGHTS_PASSWORD_PEPPER
DAILY_INSIGHTS_TRUSTED_PROXY_CIDRS
DAILY_INSIGHTS_FINDB_BASE_URL
DAILY_INSIGHTS_FINDB_API_KEY
DAILY_INSIGHTS_R2_ENDPOINT_URL
DAILY_INSIGHTS_R2_BUCKET_NAME
DAILY_INSIGHTS_R2_ACCESS_KEY_ID
DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY
"
for key in $api_required_keys; do
  value=$(env_value "$api_env" "$key")
  reject_placeholder "$key" "$value"
done

if [ "$(env_value "$api_env" DAILY_INSIGHTS_ENVIRONMENT)" != "production" ]; then
  echo "API runtime environment must be production" >&2
  exit 1
fi
if [ "$(env_value "$api_env" DAILY_INSIGHTS_TRUSTED_PROXY_CIDRS)" != "172.30.0.0/24" ]; then
  echo "API trusted proxy CIDR must match the pinned production app network" >&2
  exit 1
fi
database_url=$(env_value "$api_env" DAILY_INSIGHTS_DATABASE_URL)
case "$database_url" in
  postgresql+psycopg://*) ;;
  *)
    echo "production database URL must use postgresql+psycopg" >&2
    exit 1
    ;;
esac
session_secret=$(env_value "$api_env" DAILY_INSIGHTS_SESSION_SECRET)
password_pepper=$(env_value "$api_env" DAILY_INSIGHTS_PASSWORD_PEPPER)
if [ "${#session_secret}" -lt 32 ] || [ "${#password_pepper}" -lt 32 ]; then
  echo "session secret and password pepper must each contain at least 32 characters" >&2
  exit 1
fi
if [ "$session_secret" = "$password_pepper" ]; then
  echo "session secret and password pepper must differ" >&2
  exit 1
fi

require_regular_root_file "$web_env" "Web runtime env"
web_mode=$(file_mode "$web_env")
case "$web_mode" in
  600 | 640 | 644) ;;
  *)
    echo "Web runtime env must not be group/world writable" >&2
    exit 1
    ;;
esac
if [ "$(env_value "$web_env" APP_ENV)" != "production" ]; then
  echo "Web runtime environment must be production" >&2
  exit 1
fi
if grep -Eqi '(PASSWORD|SECRET|TOKEN|ACCESS_KEY|API_KEY|DATABASE_URL)=' "$web_env"; then
  echo "Web runtime env must not contain credentials" >&2
  exit 1
fi

require_regular_root_file "$allowlist_file" "Cloudflare real-IP allowlist"
python3 "$script_dir/validate-cloudflare-allowlist.py" "$allowlist_file"

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
python3 "$script_dir/validate-certificate-hostname.py" \
  "$certificate_file" "$public_hostname"
openssl x509 -in "$certificate_file" -noout -checkend "$minimum_validity_seconds" >/dev/null
openssl pkey -in "$private_key_file" -noout >/dev/null

temporary_dir=$(mktemp -d)
trap 'rm -rf "$temporary_dir"' EXIT HUP INT TERM
openssl x509 -in "$certificate_file" -pubkey -noout |
  openssl pkey -pubin -outform DER >"$temporary_dir/cert.der"
openssl pkey -in "$private_key_file" -pubout -outform DER >"$temporary_dir/key.der"
if ! cmp -s "$temporary_dir/cert.der" "$temporary_dir/key.der"; then
  echo "Origin certificate and private key do not match" >&2
  exit 1
fi

echo "production runtime preflight passed"
