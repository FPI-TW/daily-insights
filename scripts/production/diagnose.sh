#!/bin/sh
set -eu

echo "Daily Insights container status"
docker ps -a \
  --filter label=com.docker.compose.project=daily-insights-production

for container in \
  daily-insights-api \
  daily-insights-web \
  daily-insights-morning-report-scheduler \
  daily-insights-daily-news-scheduler \
  daily-insights-analyst-viewpoints-scheduler \
  daily-insights-index-daily-bars-scheduler \
  daily-insights-data-management-worker \
  daily-insights-nginx; do
  if ! docker inspect "$container" >/dev/null 2>&1; then
    echo "$container: not created"
    continue
  fi
  echo "$container runtime state"
  docker inspect --format \
    'status={{.State.Status}} running={{.State.Running}} restarting={{.State.Restarting}} exit_code={{.State.ExitCode}} oom_killed={{.State.OOMKilled}} error={{json .State.Error}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}not-configured{{end}}' \
    "$container" || true
  echo "$container recent logs"
  docker logs --tail=200 "$container" 2>&1 || true
done
