#!/bin/sh

production_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
production_compose_file="$production_root/compose.production.yaml"
production_project_name="daily-insights-production"
production_state_dir="${DAILY_INSIGHTS_STATE_DIR:-/var/lib/daily-insights}"
production_current_release="$production_state_dir/current.env"
production_previous_release="$production_state_dir/previous.env"

release_value() {
  release_file=$1
  release_key=$2
  sed -n "s/^${release_key}=//p" "$release_file"
}

compose_with_release() {
  release_file=$1
  shift
  docker compose \
    --project-name "$production_project_name" \
    --env-file "$release_file" \
    --file "$production_compose_file" \
    "$@"
}

atomic_install_release() {
  source_file=$1
  destination_file=$2
  temporary_file="${destination_file}.tmp.$$"

  cp "$source_file" "$temporary_file"
  chmod 0644 "$temporary_file"
  mv "$temporary_file" "$destination_file"
}
