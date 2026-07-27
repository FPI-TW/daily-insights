#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/common.sh"

candidate_release=${1:-}
if [ -z "$candidate_release" ]; then
  echo "usage: $0 CANDIDATE_RELEASE_ENV" >&2
  exit 2
fi
"$script_dir/validate-release.sh" "$candidate_release"
"$script_dir/preflight.sh" "$candidate_release"

if [ ! -d "$production_state_dir" ]; then
  echo "$production_state_dir must be provisioned before deployment" >&2
  exit 1
fi

lock_dir="$production_state_dir/deploy.lock"
if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "another production lifecycle operation is active" >&2
  exit 1
fi
cleanup_lock() {
  rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup_lock EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

candidate_copy="$production_state_dir/candidate.env"
atomic_install_release "$candidate_release" "$candidate_copy"

compose_with_release "$candidate_copy" config --quiet
"$script_dir/login-registries.sh" "$candidate_copy"
compose_with_release "$candidate_copy" pull

# Migrations must remain forward-compatible with the previous application.
# Database restore is never used as a routine application rollback.
compose_with_release "$candidate_copy" run --rm --no-deps api alembic upgrade head

had_previous=false
if [ -f "$production_current_release" ]; then
  "$script_dir/validate-release.sh" "$production_current_release"
  atomic_install_release "$production_current_release" "$production_previous_release"
  had_previous=true
fi
atomic_install_release "$candidate_copy" "$production_current_release"

if systemctl restart daily-insights.service &&
  "$script_dir/health.sh" "$production_current_release"; then
  rm -f "$candidate_copy"
  echo "deployment completed"
  exit 0
fi

echo "deployment failed health validation" >&2
systemctl stop daily-insights.service || true
compose_with_release "$production_current_release" down --remove-orphans || true
if [ "$had_previous" = true ]; then
  echo "restoring previous application release" >&2
  atomic_install_release "$production_previous_release" "$production_current_release"
  if systemctl restart daily-insights.service &&
    "$script_dir/health.sh" "$production_current_release"; then
    exit 1
  fi
  systemctl stop daily-insights.service || true
  compose_with_release "$production_current_release" down --remove-orphans || true
  echo "previous release also failed recovery health; service is stopped" >&2
  exit 1
fi

failed_release="$production_state_dir/failed.env"
atomic_install_release "$production_current_release" "$failed_release"
rm -f "$production_current_release" "$candidate_copy"
echo "first deployment failed; known-bad release retained at $failed_release and service is stopped" >&2
exit 1
