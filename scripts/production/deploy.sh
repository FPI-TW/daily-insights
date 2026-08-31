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

required_environment="
API_IMAGE
WEB_IMAGE
PUBLIC_HOSTNAME
DAILY_INSIGHTS_DATABASE_URL
DAILY_INSIGHTS_SESSION_SECRET
DAILY_INSIGHTS_PASSWORD_PEPPER
DAILY_INSIGHTS_MORNING_REPORTS_ENABLED
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
    DAILY_INSIGHTS_TWELVE_DATA_API_KEY \
    DAILY_INSIGHTS_TWELVE_DATA_MANIFEST_APPROVED_HASH; do
    if [ -z "$(printenv "$name" 2>/dev/null || true)" ]; then
      echo "enabled morning reports require deployment environment: $name" >&2
      exit 1
    fi
  done
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

# Migrations must remain forward-compatible with the containers serving the
# previous application version during rollout.
compose run --rm --no-deps api alembic upgrade head

if ! compose up -d --no-build --remove-orphans; then
  "$script_dir/diagnose.sh" >&2
  exit 1
fi

if ! "$script_dir/health.sh"; then
  "$script_dir/diagnose.sh" >&2
  exit 1
fi

echo "deployment completed"
