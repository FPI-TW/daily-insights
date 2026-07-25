#!/bin/sh
set -eu

fixture_root=${1:-}
if [ -z "$fixture_root" ] || [ ! -d "$fixture_root" ]; then
  echo "usage: $0 PREPARED_FIXTURE_ROOT" >&2
  exit 2
fi

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
stub_dir="$fixture_root/lifecycle-stubs"
mkdir "$stub_dir"

cat >"$stub_dir/systemctl" <<'EOF'
#!/bin/sh
echo "systemctl $*" >>"$LIFECYCLE_LOG"
if [ "${1:-}" = "restart" ]; then
  count=0
  if [ -f "$SYSTEMCTL_COUNTER" ]; then
    count=$(cat "$SYSTEMCTL_COUNTER")
  fi
  count=$((count + 1))
  echo "$count" >"$SYSTEMCTL_COUNTER"
  case "$SYSTEMCTL_MODE" in
    always-fail) exit 1 ;;
    fail-first)
      if [ "$count" -eq 1 ]; then exit 1; fi
      ;;
  esac
fi
exit 0
EOF

cat >"$stub_dir/docker" <<'EOF'
#!/bin/sh
echo "docker $*" >>"$LIFECYCLE_LOG"
case " $* " in
  *" ps -q "*)
    echo "healthy-container"
    ;;
  *" pull "*)
    if [ "${DOCKER_SIGNAL_ON_PULL:-0}" = "1" ]; then
      kill -TERM "$PPID"
    fi
    ;;
esac
if [ "${1:-}" = "inspect" ]; then
  echo healthy
fi
exit 0
EOF

cp "$fixture_root/stubs/stat" "$stub_dir/stat"
cat >"$stub_dir/openssl" <<'EOF'
#!/bin/sh
case " $* " in
  *" -pubkey -noout "*) echo "PUBLIC KEY" ;;
  *" -pubout -outform DER "*) echo "MATCHING-DER" ;;
  *" -pubin -outform DER "*) echo "MATCHING-DER" ;;
esac
exit 0
EOF
chmod +x "$stub_dir"/*

restore_runtime_fixtures() {
  cp "$fixture_root/runtime/api.valid" "$fixture_root/runtime/api.env"
  printf '%s\n' 'set_real_ip_from 104.16.0.0/13;' >"$fixture_root/config/cloudflare-realip.conf"
}

restore_runtime_fixtures
first_state="$fixture_root/first-state"
mkdir "$first_state"
: >"$fixture_root/first.log"
if PATH="$stub_dir:$PATH" \
  DAILY_INSIGHTS_STATE_DIR="$first_state" \
  LIFECYCLE_LOG="$fixture_root/first.log" \
  SYSTEMCTL_COUNTER="$fixture_root/first.counter" \
  SYSTEMCTL_MODE=always-fail \
  "$root_dir/scripts/production/deploy.sh" "$fixture_root/compose.env" >/dev/null 2>&1; then
  echo "first deployment failure fixture unexpectedly succeeded" >&2
  exit 1
fi
[ ! -e "$first_state/current.env" ]
[ -f "$first_state/failed.env" ]
grep -q 'systemctl stop daily-insights.service' "$fixture_root/first.log"
grep -q ' down --remove-orphans' "$fixture_root/first.log"

restore_runtime_fixtures
rollback_state="$fixture_root/rollback-state"
mkdir "$rollback_state"
cp "$fixture_root/compose.env" "$rollback_state/current.env"
sed 's/sha256:a/sha256:b/g' \
  "$fixture_root/compose.env" >"$rollback_state/previous.env"
cp "$rollback_state/current.env" "$fixture_root/original-current.env"
: >"$fixture_root/rollback.log"
if PATH="$stub_dir:$PATH" \
  DAILY_INSIGHTS_STATE_DIR="$rollback_state" \
  LIFECYCLE_LOG="$fixture_root/rollback.log" \
  SYSTEMCTL_COUNTER="$fixture_root/rollback.counter" \
  SYSTEMCTL_MODE=fail-first \
  "$root_dir/scripts/production/rollback.sh" >/dev/null 2>&1; then
  echo "failed rollback target fixture unexpectedly succeeded" >&2
  exit 1
fi
cmp "$rollback_state/current.env" "$fixture_root/original-current.env"
grep -q 'systemctl stop daily-insights.service' "$fixture_root/rollback.log"
grep -q ' down --remove-orphans' "$fixture_root/rollback.log"
[ "$(cat "$fixture_root/rollback.counter")" -eq 2 ]

restore_runtime_fixtures
deploy_recovery_state="$fixture_root/deploy-recovery-state"
mkdir "$deploy_recovery_state"
sed 's/sha256:a/sha256:b/g' \
  "$fixture_root/compose.env" >"$deploy_recovery_state/current.env"
cp "$deploy_recovery_state/current.env" "$fixture_root/deploy-previous.env"
: >"$fixture_root/deploy-recovery.log"
if PATH="$stub_dir:$PATH" \
  DAILY_INSIGHTS_STATE_DIR="$deploy_recovery_state" \
  LIFECYCLE_LOG="$fixture_root/deploy-recovery.log" \
  SYSTEMCTL_COUNTER="$fixture_root/deploy-recovery.counter" \
  SYSTEMCTL_MODE=always-fail \
  "$root_dir/scripts/production/deploy.sh" "$fixture_root/compose.env" >/dev/null 2>&1; then
  echo "always-failing previous-release recovery unexpectedly succeeded" >&2
  exit 1
fi
cmp "$deploy_recovery_state/current.env" "$fixture_root/deploy-previous.env"
[ "$(grep -c 'systemctl stop daily-insights.service' "$fixture_root/deploy-recovery.log")" -eq 2 ]
[ "$(grep -c ' down --remove-orphans' "$fixture_root/deploy-recovery.log")" -eq 2 ]

restore_runtime_fixtures
rollback_failure_state="$fixture_root/rollback-failure-state"
mkdir "$rollback_failure_state"
cp "$fixture_root/compose.env" "$rollback_failure_state/current.env"
sed 's/sha256:a/sha256:b/g' \
  "$fixture_root/compose.env" >"$rollback_failure_state/previous.env"
cp "$rollback_failure_state/current.env" "$fixture_root/rollback-original.env"
: >"$fixture_root/rollback-failure.log"
if PATH="$stub_dir:$PATH" \
  DAILY_INSIGHTS_STATE_DIR="$rollback_failure_state" \
  LIFECYCLE_LOG="$fixture_root/rollback-failure.log" \
  SYSTEMCTL_COUNTER="$fixture_root/rollback-failure.counter" \
  SYSTEMCTL_MODE=always-fail \
  "$root_dir/scripts/production/rollback.sh" >/dev/null 2>&1; then
  echo "always-failing rollback recovery unexpectedly succeeded" >&2
  exit 1
fi
cmp "$rollback_failure_state/current.env" "$fixture_root/rollback-original.env"
[ "$(grep -c 'systemctl stop daily-insights.service' "$fixture_root/rollback-failure.log")" -eq 2 ]
[ "$(grep -c ' down --remove-orphans' "$fixture_root/rollback-failure.log")" -eq 2 ]

restore_runtime_fixtures
signal_state="$fixture_root/signal-state"
mkdir "$signal_state"
: >"$fixture_root/signal.log"
if PATH="$stub_dir:$PATH" \
  DAILY_INSIGHTS_STATE_DIR="$signal_state" \
  LIFECYCLE_LOG="$fixture_root/signal.log" \
  SYSTEMCTL_COUNTER="$fixture_root/signal.counter" \
  SYSTEMCTL_MODE=always-fail \
  DOCKER_SIGNAL_ON_PULL=1 \
  "$root_dir/scripts/production/deploy.sh" "$fixture_root/compose.env" >/dev/null 2>&1; then
  echo "signal-interrupted deployment unexpectedly succeeded" >&2
  exit 1
fi
[ ! -d "$signal_state/deploy.lock" ]
[ ! -e "$signal_state/current.env" ]
if grep -q ' run --rm ' "$fixture_root/signal.log" ||
  grep -q '^systemctl ' "$fixture_root/signal.log"; then
  echo "signal-interrupted deployment continued mutating lifecycle state" >&2
  exit 1
fi
