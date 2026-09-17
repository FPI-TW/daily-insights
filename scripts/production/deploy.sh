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
  if quiesce_schema_boundary_services; then
    echo "Legacy schedulers are confirmed quiescent. Inspect the diagnostics, correct the failure, then rerun deploy.sh; do not start orchestration-dispatcher before orchestration-worker is healthy." >&2
  else
    echo "Legacy schedulers could not be confirmed quiescent. Keep the deployment halted, stop every legacy scheduler and data-management-worker manually, inspect the diagnostics, then rerun deploy.sh." >&2
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

quiesce_schema_boundary_services() {
  quiesce_failed=false
  for container in \
    daily-insights-api \
    daily-insights-orchestration-dispatcher \
    daily-insights-orchestration-worker \
    daily-insights-morning-report-scheduler \
    daily-insights-daily-news-scheduler \
    daily-insights-analyst-viewpoints-scheduler \
    daily-insights-index-daily-bars-scheduler \
    daily-insights-institutional-flows-scheduler \
    daily-insights-macro-dashboard-scheduler \
    daily-insights-data-management-worker; do
    if docker inspect "$container" >/dev/null 2>&1 && ! docker stop "$container"; then
      echo "failed to stop $container" >&2
      quiesce_failed=true
    fi
    if ! confirm_stopped "$container"; then
      quiesce_failed=true
    fi
  done
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
DAILY_INSIGHTS_ORCHESTRATION_ENABLED
DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE
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

if [ "$DAILY_INSIGHTS_ORCHESTRATION_ENABLED" != true ]; then
  echo "DAILY_INSIGHTS_ORCHESTRATION_ENABLED must be true for unified cutover" >&2
  exit 1
fi
taipei_now=${DAILY_INSIGHTS_CUTOVER_TAIPEI_NOW:-$(TZ=Asia/Taipei date +%FT%T%z)}
taipei_date=${taipei_now%%T*}
if expected_activation_date=$(TZ=Asia/Taipei date -d "$taipei_date +1 day" +%F 2>/dev/null); then
  :
else
  expected_activation_date=$(TZ=Asia/Taipei date -j -v+1d -f "%Y-%m-%d" "$taipei_date" +%F)
fi

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
  [ -z "$(printenv DAILY_INSIGHTS_NEWS_MODEL_API_KEY 2>/dev/null || true)" ]; then
  echo "enabled daily news requires deployment environment: DAILY_INSIGHTS_NEWS_MODEL_API_KEY" >&2
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

# The after-10:00/next-day guard is a one-time cutover invariant. A retry after
# migration but before the first routine stays in activation-pending state.
# Once a routine exists, later releases preserve the original activation date.
cutover_state=0
compose run --rm --no-deps api \
  python -m daily_insights_api.scripts.check_orchestration_cutover || cutover_state=$?
case "$cutover_state" in
  0)
    if [ "$DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE" \> "$taipei_date" ]; then
      echo "steady-state orchestration activation date must not be in the future" >&2
      exit 1
    fi
    ;;
  10)
    taipei_time=${taipei_now#*T}
    taipei_hour=${taipei_time%%:*}
    if [ "$taipei_hour" -lt 10 ]; then
      echo "unified orchestration cutover must run after 10:00 Asia/Taipei" >&2
      exit 1
    fi
    if [ "$DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE" != "$expected_activation_date" ]; then
      echo "DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE must be the next Taipei date: $expected_activation_date" >&2
      exit 1
    fi
    ;;
  11)
    if [ "$DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE" \< "$taipei_date" ] ||
      [ "$DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE" \> "$expected_activation_date" ]; then
      echo "activation-pending orchestration date must be today or the next Taipei date" >&2
      exit 1
    fi
    ;;
  *)
    echo "could not determine unified orchestration cutover state" >&2
    exit 1
    ;;
esac

# Validate the official-entrypoint-rendered template before replacing the
# production proxy. Recreate nginx while the old upstreams are still present so
# it is using Docker's runtime resolver before API/Web receive new addresses.
compose run --rm --no-deps nginx nginx -t
compose up -d --no-build --force-recreate --no-deps nginx

# Neither API mutations nor any old/new worker may cross the schema boundary.
if ! quiesce_schema_boundary_services; then
  diagnose_cutover_failure "schema-boundary services could not be confirmed quiescent; migration was not attempted"
fi

legacy_queue_state=0
compose run --rm --no-deps api \
  python -m daily_insights_api.scripts.check_legacy_queues_quiescent || legacy_queue_state=$?
if [ "$legacy_queue_state" -ne 0 ]; then
  diagnose_cutover_failure "legacy queues still contain pending or running work; migration was not attempted"
fi

if ! compose run --rm --no-deps api alembic upgrade head; then
  diagnose_cutover_failure "database migration failed after schema-boundary services were stopped"
fi

# Only the unified worker may observe rows created under the new schema. Keep
# the dispatcher stopped until that worker is confirmed healthy.
if ! compose up -d --no-build --force-recreate --no-deps orchestration-worker; then
  diagnose_cutover_failure "orchestration-worker failed to start"
fi
if ! wait_for_healthy_container daily-insights-orchestration-worker; then
  diagnose_cutover_failure "orchestration-worker did not become healthy"
fi

if ! compose up -d --no-build --remove-orphans api web orchestration-dispatcher; then
  diagnose_cutover_failure "final service convergence failed after the replacement worker started"
fi

if ! "$script_dir/health.sh"; then
  diagnose_cutover_failure "final production health check failed"
fi

echo "deployment completed"
