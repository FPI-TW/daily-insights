#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
config_file=${1:-/etc/daily-insights/ssm.env}
release_file=${2:-/var/lib/daily-insights/current.env}

if [ ! -f "$config_file" ] || [ -L "$config_file" ]; then
  echo "SSM configuration must be a regular non-symlink file: $config_file" >&2
  exit 1
fi

file_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

file_owner_uid() {
  stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1"
}

if [ "$(file_owner_uid "$config_file")" != "0" ]; then
  echo "SSM configuration must be owned by root: $config_file" >&2
  exit 1
fi
case "$(file_mode "$config_file")" in
  600 | 640 | 644) ;;
  *)
    echo "SSM configuration must not be group/world writable: $config_file" >&2
    exit 1
    ;;
esac
if grep -Ev '^(#.*|[[:space:]]*|(AWS_REGION|DAILY_INSIGHTS_SSM_API_ENV_PATH)=[^[:cntrl:]]+)$' "$config_file"; then
  echo "SSM configuration contains a malformed or unsupported line" >&2
  exit 1
fi
for config_key in AWS_REGION DAILY_INSIGHTS_SSM_API_ENV_PATH; do
  config_count=$(sed -n "/^${config_key}=/p" "$config_file" | wc -l | tr -d ' ')
  if [ "$config_count" -ne 1 ]; then
    echo "SSM configuration must define $config_key exactly once" >&2
    exit 1
  fi
done

"$script_dir/validate-release.sh" "$release_file"

AWS_REGION=$(sed -n 's/^AWS_REGION=//p' "$config_file")
DAILY_INSIGHTS_SSM_API_ENV_PATH=$(sed -n 's/^DAILY_INSIGHTS_SSM_API_ENV_PATH=//p' "$config_file")
if ! printf '%s\n' "$AWS_REGION" | grep -Eq '^[a-z]{2}(-gov)?-[a-z]+-[0-9]+$'; then
  echo "AWS_REGION is malformed" >&2
  exit 1
fi
case "$DAILY_INSIGHTS_SSM_API_ENV_PATH" in
  /*) ;;
  *)
    echo "DAILY_INSIGHTS_SSM_API_ENV_PATH must be an absolute parameter path" >&2
    exit 1
    ;;
esac
if printf '%s\n' "$DAILY_INSIGHTS_SSM_API_ENV_PATH" | grep -Eq '[[:space:]]|(^|/)\.\.?(/|$)'; then
  echo "DAILY_INSIGHTS_SSM_API_ENV_PATH contains unsafe path segments" >&2
  exit 1
fi

runtime_dir=$(sed -n 's/^DAILY_INSIGHTS_RUNTIME_DIR=//p' "$release_file")
exec python3 "$script_dir/materialize-runtime-env.py" \
  --region "$AWS_REGION" \
  --parameter-path "$DAILY_INSIGHTS_SSM_API_ENV_PATH" \
  --runtime-dir "$runtime_dir"
