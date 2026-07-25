#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/common.sh"

if [ ! -f "$production_current_release" ] ||
  [ ! -f "$production_previous_release" ]; then
  echo "both current and previous release manifests are required" >&2
  exit 1
fi
"$script_dir/validate-release.sh" "$production_current_release"
"$script_dir/validate-release.sh" "$production_previous_release"

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

current_copy="$production_state_dir/rollback-current.env"
atomic_install_release "$production_current_release" "$current_copy"
atomic_install_release "$production_previous_release" "$production_current_release"

if systemctl restart daily-insights.service &&
  "$script_dir/health.sh" "$production_current_release"; then
  atomic_install_release "$current_copy" "$production_previous_release"
  rm -f "$current_copy"
  echo "rollback completed"
  exit 0
fi

echo "rollback target failed health validation; restoring original release" >&2
systemctl stop daily-insights.service || true
compose_with_release "$production_current_release" down --remove-orphans || true
atomic_install_release "$current_copy" "$production_current_release"
if systemctl restart daily-insights.service &&
  "$script_dir/health.sh" "$production_current_release"; then
  rm -f "$current_copy"
  exit 1
fi
systemctl stop daily-insights.service || true
compose_with_release "$production_current_release" down --remove-orphans || true
echo "original release also failed recovery health; service is stopped" >&2
exit 1
