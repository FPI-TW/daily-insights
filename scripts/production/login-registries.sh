#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/common.sh"

release_file=${1:-$production_current_release}
"$script_dir/validate-release.sh" "$release_file"

seen_registries=
for image_key in API_IMAGE WEB_IMAGE; do
  image=$(release_value "$release_file" "$image_key")
  registry=${image%%/*}
  case "$registry" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].dkr.ecr.*.amazonaws.com)
      ;;
    *)
      continue
      ;;
  esac
  case " $seen_registries " in
    *" $registry "*) continue ;;
  esac

  region=${registry#*.dkr.ecr.}
  region=${region%.amazonaws.com}
  if ! printf '%s\n' "$region" | grep -Eq '^[a-z]{2}(-gov)?-[a-z]+-[0-9]+$'; then
    echo "unable to derive an AWS region from ECR registry: $registry" >&2
    exit 1
  fi

  password=$(AWS_PAGER= aws ecr get-login-password --region "$region")
  if [ -z "$password" ]; then
    echo "Amazon ECR returned an empty login password" >&2
    exit 1
  fi
  printf '%s' "$password" |
    docker login --username AWS --password-stdin "$registry" >/dev/null
  seen_registries="$seen_registries $registry"
done

echo "production container registries authenticated"
