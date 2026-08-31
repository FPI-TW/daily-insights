#!/bin/sh
set -eu

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
containers="daily-insights-api daily-insights-web daily-insights-nginx daily-insights-morning-report-scheduler"

while [ "$(date +%s)" -le "$deadline" ]; do
  all_healthy=true
  for container in $containers; do
    state=$(
      docker inspect \
        --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "$container" 2>/dev/null || true
    )
    runtime_status=${state%% *}
    health_status=${state#* }

    case "$runtime_status" in
      dead | exited | paused | removing | restarting)
        echo "$container entered terminal runtime state: $runtime_status" >&2
        exit 1
        ;;
    esac

    if [ "$health_status" != "healthy" ]; then
      all_healthy=false
    fi
  done

  if [ "$all_healthy" = true ]; then
    echo "production containers are healthy: $containers"
    exit 0
  fi
  sleep 5
done

docker ps -a \
  --filter label=com.docker.compose.project=daily-insights-production >&2
echo "production health did not converge within ${timeout_seconds}s" >&2
exit 1
