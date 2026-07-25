#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/common.sh"

release_file=${1:-$production_current_release}
"$script_dir/validate-release.sh" "$release_file"

timeout_seconds=${DAILY_INSIGHTS_HEALTH_TIMEOUT_SECONDS:-240}
case "$timeout_seconds" in
  '' | *[!0-9]*)
    echo "DAILY_INSIGHTS_HEALTH_TIMEOUT_SECONDS must be a positive integer" >&2
    exit 2
    ;;
esac
if [ "$timeout_seconds" -lt 1 ] || [ "$timeout_seconds" -gt 900 ]; then
  echo "health timeout must be between 1 and 900 seconds" >&2
  exit 2
fi

deadline=$(( $(date +%s) + timeout_seconds ))
services="api web nginx"

while [ "$(date +%s)" -le "$deadline" ]; do
  all_healthy=true
  for service in $services; do
    container_id=$(compose_with_release "$release_file" ps -q "$service" 2>/dev/null || true)
    if [ -z "$container_id" ]; then
      all_healthy=false
      continue
    fi
    status=$(
      docker inspect \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "$container_id" 2>/dev/null || true
    )
    if [ "$status" != "healthy" ]; then
      all_healthy=false
    fi
  done

  if [ "$all_healthy" = true ]; then
    echo "production services are healthy: $services"
    exit 0
  fi
  sleep 5
done

compose_with_release "$release_file" ps >&2
echo "production health did not converge within ${timeout_seconds}s" >&2
exit 1
