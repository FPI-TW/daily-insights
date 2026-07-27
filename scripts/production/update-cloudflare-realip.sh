#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target_file=${1:-/etc/daily-insights/cloudflare-realip.conf}
target_dir=$(dirname "$target_file")

if [ "$(id -u)" -ne 0 ]; then
  echo "Cloudflare allowlist update must run as root" >&2
  exit 1
fi
if [ ! -d "$target_dir" ] || [ -L "$target_dir" ]; then
  echo "Cloudflare allowlist target directory is missing or unsafe: $target_dir" >&2
  exit 1
fi

temporary_dir=$(mktemp -d)
trap 'rm -rf "$temporary_dir"' EXIT HUP INT TERM

curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  https://www.cloudflare.com/ips-v4 >"$temporary_dir/ips-v4"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  https://www.cloudflare.com/ips-v6 >"$temporary_dir/ips-v6"

{
  printf '# Generated from Cloudflare published IP ranges. Do not edit manually.\n'
  while IFS= read -r cidr; do
    [ -n "$cidr" ] && printf 'set_real_ip_from %s;\n' "$cidr"
  done <"$temporary_dir/ips-v4"
  while IFS= read -r cidr; do
    [ -n "$cidr" ] && printf 'set_real_ip_from %s;\n' "$cidr"
  done <"$temporary_dir/ips-v6"
} >"$temporary_dir/cloudflare-realip.conf"

python3 "$script_dir/validate-cloudflare-allowlist.py" \
  "$temporary_dir/cloudflare-realip.conf"
temporary_target=$(mktemp "$target_dir/.cloudflare-realip.XXXXXX")
trap 'rm -rf "$temporary_dir"; rm -f "$temporary_target"' EXIT HUP INT TERM
install -o root -g root -m 0644 \
  "$temporary_dir/cloudflare-realip.conf" "$temporary_target"
mv -f "$temporary_target" "$target_file"
echo "Cloudflare real-IP allowlist updated; restart daily-insights.service to apply it"
