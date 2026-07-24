#!/bin/sh
set -eu

container_id="$(
  docker run --rm --detach \
    -e POSTGRES_DB=daily_insights_test \
    -e POSTGRES_USER=daily_insights \
    -e POSTGRES_PASSWORD=daily_insights \
    -p 127.0.0.1::5432 \
    postgres:17-alpine
)"

cleanup() {
  docker stop "$container_id" >/dev/null
}
trap cleanup EXIT HUP INT TERM

attempt=0
until docker exec "$container_id" pg_isready -U daily_insights -d daily_insights_test >/dev/null
do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "隔離 PostgreSQL 未能在 30 秒內啟動。" >&2
    exit 1
  fi
  sleep 1
done

database_port="$(
  docker port "$container_id" 5432/tcp |
    sed -E 's/.*:([0-9]+)$/\1/' |
    head -n 1
)"
if [ -z "$database_port" ]; then
  echo "無法取得隔離 PostgreSQL 的連接埠。" >&2
  exit 1
fi

database_url="postgresql+psycopg://daily_insights:daily_insights@127.0.0.1:${database_port}/daily_insights_test"

DAILY_INSIGHTS_DATABASE_URL="$database_url" \
  uv run --project apps/api alembic -c apps/api/alembic.ini upgrade head
DAILY_INSIGHTS_DATABASE_URL="$database_url" \
  uv run --project apps/api alembic -c apps/api/alembic.ini check

immutable_trigger_count="$(
  docker exec "$container_id" \
    psql -U daily_insights -d daily_insights_test -Atc \
    "SELECT count(*) FROM pg_trigger WHERE tgname IN ('report_publications_are_immutable', 'publication_source_runs_are_immutable') AND NOT tgisinternal"
)"
if [ "$immutable_trigger_count" -ne 2 ]; then
  echo "Phase 2 immutable publication triggers 未正確建立。" >&2
  exit 1
fi

phase2b_trigger_count="$(
  docker exec "$container_id" \
    psql -U daily_insights -d daily_insights_test -Atc \
    "SELECT count(*) FROM pg_trigger WHERE tgname IN ('conversations_retained_history', 'messages_retained_history', 'generation_records_retained_history', 'model_configurations_retained_history') AND NOT tgisinternal"
)"
if [ "$phase2b_trigger_count" -ne 4 ]; then
  echo "Phase 2B retained history triggers 未正確建立。" >&2
  exit 1
fi

phase2b_table_count="$(
  docker exec "$container_id" \
    psql -U daily_insights -d daily_insights_test -Atc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('active_model_configuration', 'asset_migration_manifests', 'asset_migration_entries', 'podcast_episodes', 'podcast_episode_translations', 'podcast_episode_audio_variants')"
)"
if [ "$phase2b_table_count" -ne 6 ]; then
  echo "Phase 2B foundation tables 未正確建立。" >&2
  exit 1
fi

DAILY_INSIGHTS_DATABASE_URL="$database_url" \
  uv run --project apps/api alembic -c apps/api/alembic.ini downgrade 20260724_0002

phase2b_downgrade_table_count="$(
  docker exec "$container_id" \
    psql -U daily_insights -d daily_insights_test -Atc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('active_model_configuration', 'asset_migration_manifests', 'asset_migration_entries', 'podcast_episodes', 'podcast_episode_translations', 'podcast_episode_audio_variants')"
)"
phase2b_downgrade_trigger_count="$(
  docker exec "$container_id" \
    psql -U daily_insights -d daily_insights_test -Atc \
    "SELECT count(*) FROM pg_trigger WHERE tgname IN ('conversations_retained_history', 'messages_retained_history', 'generation_records_retained_history', 'model_configurations_retained_history') AND NOT tgisinternal"
)"
if [ "$phase2b_downgrade_table_count" -ne 0 ] || [ "$phase2b_downgrade_trigger_count" -ne 0 ]; then
  echo "Phase 2B downgrade 後仍殘留 foundation tables 或 retained history triggers。" >&2
  exit 1
fi

DAILY_INSIGHTS_DATABASE_URL="$database_url" \
  uv run --project apps/api alembic -c apps/api/alembic.ini upgrade head
DAILY_INSIGHTS_DATABASE_URL="$database_url" \
  uv run --project apps/api alembic -c apps/api/alembic.ini check

DAILY_INSIGHTS_TEST_DATABASE_URL="$database_url" \
  pnpm test
