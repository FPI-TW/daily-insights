#!/bin/sh
set -eu

allowlist_file=${1:-}
if [ -z "$allowlist_file" ] || [ ! -f "$allowlist_file" ]; then
  echo "usage: $0 CLOUDFLARE_ALLOWLIST" >&2
  exit 2
fi

if grep -Eq 'set_real_ip_from[[:space:]]+(0\.0\.0\.0/0|::/0);' "$allowlist_file"; then
  echo "Cloudflare allowlist must not contain a default route" >&2
  exit 1
fi
if grep -Eqi 'set_real_ip_from[[:space:]]+(10\.|127\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|fc|fd|fe80:)' "$allowlist_file"; then
  echo "Cloudflare allowlist contains a private, link-local, or documentation range" >&2
  exit 1
fi

entry_count=$(
  awk '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    $1 != "set_real_ip_from" || NF != 2 || $2 !~ /;$/ { exit 2 }
    {
      cidr = $2
      sub(/;$/, "", cidr)
      if (cidr !~ /^[0-9A-Fa-f:.]+\/[0-9][0-9]*$/) { exit 2 }
      count += 1
    }
    END {
      if (count == 0) { exit 3 }
      print count
    }
  ' "$allowlist_file"
) || {
  echo "Cloudflare allowlist contains malformed directives or no CIDRs" >&2
  exit 1
}

if [ "$entry_count" -lt 2 ]; then
  echo "Cloudflare allowlist must contain both published IPv4 and IPv6 ranges" >&2
  exit 1
fi
if ! grep -Eq '^set_real_ip_from [0-9]+\.' "$allowlist_file" ||
  ! grep -Eq '^set_real_ip_from [0-9A-Fa-f]*:' "$allowlist_file"; then
  echo "Cloudflare allowlist must contain both IPv4 and IPv6 ranges" >&2
  exit 1
fi
