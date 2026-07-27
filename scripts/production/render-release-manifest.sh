#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$#" -ne 4 ]; then
  echo "usage: $0 API_IMAGE WEB_IMAGE NGINX_IMAGE PUBLIC_HOSTNAME" >&2
  exit 2
fi

api_image=$1
web_image=$2
nginx_image=$3
public_hostname=$4

temporary_file=$(mktemp)
trap 'rm -f "$temporary_file"' EXIT HUP INT TERM

{
  printf 'API_IMAGE=%s\n' "$api_image"
  printf 'WEB_IMAGE=%s\n' "$web_image"
  printf 'NGINX_IMAGE=%s\n' "$nginx_image"
  printf 'PUBLIC_HOSTNAME=%s\n' "$public_hostname"
  printf 'DAILY_INSIGHTS_CONFIG_DIR=/etc/daily-insights\n'
  printf 'DAILY_INSIGHTS_RUNTIME_DIR=/run/daily-insights\n'
} >"$temporary_file"

"$script_dir/validate-release.sh" "$temporary_file"
cat "$temporary_file"
