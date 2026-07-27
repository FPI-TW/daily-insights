#!/usr/bin/env python3
"""Materialize production runtime env files from one SSM parameter path."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping

REQUIRED_API_KEYS = {
    "DAILY_INSIGHTS_ENVIRONMENT",
    "DAILY_INSIGHTS_DATABASE_URL",
    "DAILY_INSIGHTS_SESSION_SECRET",
    "DAILY_INSIGHTS_PASSWORD_PEPPER",
    "DAILY_INSIGHTS_TRUSTED_PROXY_CIDRS",
    "DAILY_INSIGHTS_FINDB_BASE_URL",
    "DAILY_INSIGHTS_FINDB_API_KEY",
    "DAILY_INSIGHTS_R2_ENDPOINT_URL",
    "DAILY_INSIGHTS_R2_BUCKET_NAME",
    "DAILY_INSIGHTS_R2_ACCESS_KEY_ID",
    "DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY",
}
OPTIONAL_API_KEYS = {
    "DAILY_INSIGHTS_R2_SIGNED_URL_TTL_SECONDS",
}
ALLOWED_API_KEYS = REQUIRED_API_KEYS | OPTIONAL_API_KEYS
ENV_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True)
    parser.add_argument("--parameter-path", required=True)
    parser.add_argument("--runtime-dir", required=True, type=pathlib.Path)
    return parser.parse_args()


def load_parameters(region: str, parameter_path: str) -> Mapping[str, str]:
    normalized_path = f"{parameter_path.rstrip('/')}/"
    result = subprocess.run(
        [
            "aws",
            "ssm",
            "get-parameters-by-path",
            "--region",
            region,
            "--path",
            normalized_path,
            "--recursive",
            "--with-decryption",
            "--output",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "AWS_PAGER": ""},
    )
    payload = json.loads(result.stdout)
    parameters = payload.get("Parameters")
    if not isinstance(parameters, list):
        raise ValueError("AWS CLI response does not contain a Parameters list")

    values: dict[str, str] = {}
    for parameter in parameters:
        if not isinstance(parameter, dict):
            raise ValueError("AWS CLI returned a malformed parameter")
        name = parameter.get("Name")
        value = parameter.get("Value")
        if not isinstance(name, str) or not name.startswith(normalized_path):
            raise ValueError("AWS CLI returned a parameter outside the configured path")
        key = name.removeprefix(normalized_path)
        if "/" in key or not ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"parameter must be a direct env-key child: {name}")
        if key in values:
            raise ValueError(f"duplicate parameter key: {key}")
        if not isinstance(value, str) or not value:
            raise ValueError(f"parameter value must be non-empty: {name}")
        if "\n" in value or "\r" in value or "\0" in value:
            raise ValueError(f"parameter value contains an unsupported control character: {name}")
        if re.search(r"""[\s'"#]""", value):
            raise ValueError(f"parameter value is not safe for a Docker env file: {name}")
        values[key] = value

    missing = REQUIRED_API_KEYS - values.keys()
    unknown = values.keys() - ALLOWED_API_KEYS
    if missing:
        raise ValueError(f"missing required parameter keys: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"unsupported parameter keys: {', '.join(sorted(unknown))}")
    return values


def validate_runtime_dir(runtime_dir: pathlib.Path) -> None:
    if not runtime_dir.is_absolute():
        raise ValueError("runtime directory must be absolute")
    if runtime_dir.is_symlink():
        raise ValueError("runtime directory must not be a symlink")
    runtime_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    stat_result = runtime_dir.stat()
    if stat_result.st_uid != 0:
        raise ValueError("runtime directory must be owned by root")
    if stat_result.st_mode & 0o022:
        raise ValueError("runtime directory must not be group/world writable")


def atomic_write(path: pathlib.Path, content: str, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = pathlib.Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    if os.geteuid() != 0:
        print("runtime environment materialization must run as root", file=sys.stderr)
        return 1

    args = parse_args()
    validate_runtime_dir(args.runtime_dir)
    values = load_parameters(args.region, args.parameter_path)
    api_content = "".join(f"{key}={values[key]}\n" for key in sorted(values))
    atomic_write(args.runtime_dir / "api.env", api_content, 0o600)
    atomic_write(args.runtime_dir / "web.env", "APP_ENV=production\n", 0o644)
    print("production runtime environment materialized")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (json.JSONDecodeError, OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"runtime environment materialization failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
