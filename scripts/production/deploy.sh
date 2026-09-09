#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
compose_file="$install_root/compose.production.yaml"
project_name=daily-insights-production

compose() {
  docker compose \
    --project-name "$project_name" \
    --file "$compose_file" \
    "$@"
}

diagnose_cutover_failure() {
  message=$1
  echo "$message" >&2
  if quiesce_automatic_news; then
    echo "Automatic news is confirmed quiescent. Inspect the diagnostics, correct the failure, then rerun deploy.sh; do not start daily-news-scheduler before data-management-worker is healthy." >&2
  else
    echo "Automatic news could not be confirmed quiescent. Keep the deployment halted, stop daily-news-scheduler and data-management-worker manually, inspect the diagnostics, then rerun deploy.sh." >&2
  fi
  "$script_dir/diagnose.sh" >&2 || true
  exit 1
}

confirm_stopped() {
  container=$1
  state=$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)
  case "$state" in
    '' | created | dead | exited) return 0 ;;
    *)
      echo "$container did not stop: $state" >&2
      return 1
      ;;
  esac
}

quiesce_automatic_news() {
  quiesce_failed=false
  if ! compose stop daily-news-scheduler data-management-worker; then
    echo "failed to stop automatic news services" >&2
    quiesce_failed=true
  fi
  if ! confirm_stopped daily-insights-daily-news-scheduler; then
    quiesce_failed=true
  fi
  if ! confirm_stopped daily-insights-data-management-worker; then
    quiesce_failed=true
  fi
  [ "$quiesce_failed" = false ]
}

wait_for_healthy_container() {
  container=$1
  timeout_seconds=${DAILY_INSIGHTS_HEALTH_TIMEOUT_SECONDS:-240}
  case "$timeout_seconds" in
    '' | *[!0-9]*)
      echo "DAILY_INSIGHTS_HEALTH_TIMEOUT_SECONDS must be a positive integer" >&2
      return 1
      ;;
  esac
  if [ "$timeout_seconds" -lt 1 ] || [ "$timeout_seconds" -gt 900 ]; then
    echo "health timeout must be between 1 and 900 seconds" >&2
    return 1
  fi
  deadline=$(( $(date +%s) + timeout_seconds ))
  while [ "$(date +%s)" -le "$deadline" ]; do
    state=$(
      docker inspect \
        --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "$container" 2>/dev/null || true
    )
    runtime_status=${state%% *}
    health_status=${state#* }
    case "$runtime_status" in
      dead | exited | paused | removing | restarting) return 1 ;;
    esac
    if [ "$runtime_status" = "running" ] && [ "$health_status" = "healthy" ]; then
      return 0
    fi
    sleep 5
  done
  return 1
}

required_environment="
API_IMAGE
WEB_IMAGE
PUBLIC_HOSTNAME
DAILY_INSIGHTS_DATABASE_URL
DAILY_INSIGHTS_SESSION_SECRET
DAILY_INSIGHTS_PASSWORD_PEPPER
DAILY_INSIGHTS_MORNING_REPORTS_ENABLED
DAILY_INSIGHTS_DAILY_NEWS_ENABLED
DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED
DAILY_INSIGHTS_R2_ENDPOINT_URL
DAILY_INSIGHTS_R2_BUCKET_NAME
DAILY_INSIGHTS_R2_ACCESS_KEY_ID
DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY
DAILY_INSIGHTS_R2_SIGNED_URL_TTL_SECONDS
"
for name in $required_environment; do
  if [ -z "$(printenv "$name" 2>/dev/null || true)" ]; then
    echo "required deployment environment is missing: $name" >&2
    exit 1
  fi
done

case "$DAILY_INSIGHTS_MORNING_REPORTS_ENABLED" in
  true | false) ;;
  *)
    echo "DAILY_INSIGHTS_MORNING_REPORTS_ENABLED must be true or false" >&2
    exit 1
    ;;
esac

if [ "$DAILY_INSIGHTS_MORNING_REPORTS_ENABLED" = true ]; then
  for name in \
    DAILY_INSIGHTS_TWELVE_DATA_BASE_URL \
    DAILY_INSIGHTS_TWELVE_DATA_API_KEY; do
    if [ -z "$(printenv "$name" 2>/dev/null || true)" ]; then
      echo "enabled morning reports require deployment environment: $name" >&2
      exit 1
    fi
  done
fi

case "$DAILY_INSIGHTS_DAILY_NEWS_ENABLED" in
  true | false) ;;
  *)
    echo "DAILY_INSIGHTS_DAILY_NEWS_ENABLED must be true or false" >&2
    exit 1
    ;;
esac

if [ "$DAILY_INSIGHTS_DAILY_NEWS_ENABLED" = true ] &&
  [ -z "$(printenv DAILY_INSIGHTS_MODEL_API_KEY 2>/dev/null || true)" ]; then
  echo "enabled daily news requires deployment environment: DAILY_INSIGHTS_MODEL_API_KEY" >&2
  exit 1
fi
if [ "${DAILY_INSIGHTS_PODCAST_ANALYSIS_ENABLED:-false}" = true ] &&
  { [ -z "$(printenv DAILY_INSIGHTS_OPENAI_API_KEY 2>/dev/null || true)" ] ||
    [ -z "$(printenv DAILY_INSIGHTS_MODEL_API_KEY 2>/dev/null || true)" ]; }; then
  echo "enabled podcast analysis requires deployment environment: DAILY_INSIGHTS_OPENAI_API_KEY and DAILY_INSIGHTS_MODEL_API_KEY" >&2
  exit 1
fi

case "$DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED" in
  true | false) ;;
  *)
    echo "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED must be true or false" >&2
    exit 1
    ;;
esac

if [ "$DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED" = true ]; then
  for name in \
    DAILY_INSIGHTS_ANALYST_VIEWPOINTS_BASE_URL \
    DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY \
    DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS; do
    if [ -z "$(printenv "$name" 2>/dev/null || true)" ]; then
      echo "enabled analyst viewpoints require deployment environment: $name" >&2
      exit 1
    fi
  done
  if ! printf '%s\n' "$DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS" |
    grep -Eq '^[0-9]+([.][0-9]+)?$' ||
    ! awk -v timeout="$DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS" 'BEGIN { exit !(timeout > 0 && timeout <= 120) }'; then
    echo "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS must be greater than 0 and at most 120" >&2
    exit 1
  fi
fi

for name in API_IMAGE WEB_IMAGE; do
  value=$(printenv "$name")
  if ! printf '%s\n' "$value" |
    grep -Eq '^[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}$' ||
    printf '%s\n' "$value" | grep -Eq '@sha256:0{64}$'; then
    echo "$name must be an immutable non-placeholder image digest" >&2
    exit 1
  fi
done

sudo -n "$script_dir/preflight.sh" "$PUBLIC_HOSTNAME"
compose config --quiet
compose pull

# Validate the official-entrypoint-rendered template before replacing the
# production proxy. Recreate nginx while the old upstreams are still present so
# it is using Docker's runtime resolver before API/Web receive new addresses.
compose run --rm --no-deps nginx nginx -t
compose up -d --no-build --force-recreate --no-deps nginx

# The old direct-fetch scheduler and old queue worker must not cross the schema
# boundary: either could execute new automatic rows with predecessor semantics.
if ! quiesce_automatic_news; then
  diagnose_cutover_failure "automatic news could not be confirmed quiescent; migration was not attempted"
fi

if ! compose run --rm --no-deps api alembic upgrade head; then
  diagnose_cutover_failure "database migration failed after automatic news was stopped"
fi

# Only the replacement worker may observe rows created under the new schema.
# Keep the scheduler stopped until that worker is confirmed healthy.
if ! compose up -d --no-build --force-recreate --no-deps data-management-worker; then
  diagnose_cutover_failure "replacement data-management-worker failed to start"
fi
if ! wait_for_healthy_container daily-insights-data-management-worker; then
  diagnose_cutover_failure "replacement data-management-worker did not become healthy"
fi

if ! compose up -d --no-build --remove-orphans api web morning-report-scheduler analyst-viewpoints-scheduler index-daily-bars-scheduler institutional-flows-scheduler macro-dashboard-scheduler daily-news-scheduler; then
  diagnose_cutover_failure "final service convergence failed after the replacement worker started"
fi

if ! "$script_dir/health.sh"; then
  diagnose_cutover_failure "final production health check failed"
fi

echo "deployment completed"
