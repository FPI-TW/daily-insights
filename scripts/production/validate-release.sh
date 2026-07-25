#!/bin/sh
set -eu

release_file=${1:-}

if [ -z "$release_file" ] || [ ! -f "$release_file" ]; then
  echo "usage: $0 RELEASE_ENV" >&2
  exit 2
fi

if grep -Ev '^(#.*|[[:space:]]*|[A-Z][A-Z0-9_]*=[^[:cntrl:]]*)$' "$release_file"; then
  echo "$release_file contains a malformed release-manifest line" >&2
  exit 1
fi

defined_keys=$(sed -n 's/^\([A-Z][A-Z0-9_]*\)=.*/\1/p' "$release_file")
for defined_key in $defined_keys; do
  case "$defined_key" in
    API_IMAGE | WEB_IMAGE | NGINX_IMAGE | PUBLIC_HOSTNAME | DAILY_INSIGHTS_CONFIG_DIR | DAILY_INSIGHTS_RUNTIME_DIR) ;;
    *)
      echo "$release_file contains unsupported key: $defined_key" >&2
      exit 1
      ;;
  esac
done

read_value() {
  key=$1
  value=$(sed -n "s/^${key}=//p" "$release_file")
  count=$(sed -n "/^${key}=/p" "$release_file" | wc -l | tr -d ' ')

  if [ "$count" -ne 1 ] || [ -z "$value" ]; then
    echo "$release_file must define $key exactly once" >&2
    exit 1
  fi
  printf '%s\n' "$value"
}

for image_key in API_IMAGE WEB_IMAGE NGINX_IMAGE; do
  image_value=$(read_value "$image_key")
  if ! printf '%s\n' "$image_value" |
    grep -Eq '^[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}$'; then
    echo "$image_key must be an immutable image@sha256 digest reference" >&2
    exit 1
  fi
  if printf '%s\n' "$image_value" | grep -Eq '@sha256:0{64}$'; then
    echo "$image_key still contains an example or invalid digest" >&2
    exit 1
  fi
done

public_hostname=$(read_value PUBLIC_HOSTNAME)
if ! printf '%s\n' "$public_hostname" |
  grep -Eq '^[a-z0-9]([a-z0-9.-]*[a-z0-9])$' ||
  ! printf '%s\n' "$public_hostname" | grep -q '\.'; then
  echo "PUBLIC_HOSTNAME must be a lowercase DNS hostname" >&2
  exit 1
fi

for path_key in DAILY_INSIGHTS_CONFIG_DIR DAILY_INSIGHTS_RUNTIME_DIR; do
  path_value=$(read_value "$path_key")
  case "$path_value" in
    /*) ;;
    *)
      echo "$path_key must be an absolute path" >&2
      exit 1
      ;;
  esac
  if printf '%s\n' "$path_value" | grep -Eq '(^|/)\.\.?(/|$)|[[:space:]]'; then
    echo "$path_key must not contain traversal or whitespace" >&2
    exit 1
  fi
done

if grep -Eq '(^|_)(PASSWORD|SECRET|TOKEN|ACCESS_KEY|API_KEY)=' "$release_file"; then
  echo "release manifest must not contain secrets" >&2
  exit 1
fi
