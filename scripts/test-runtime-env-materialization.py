#!/usr/bin/env python3
"""Offline contract tests for production SSM runtime materialization."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts/production/materialize-runtime-env.py"
SPEC = importlib.util.spec_from_file_location("materialize_runtime_env", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load runtime materialization module")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def parameter_payload(overrides: dict[str, str] | None = None) -> str:
    values = {
        key: f"value-{index}"
        for index, key in enumerate(sorted(MODULE.REQUIRED_API_KEYS), start=1)
    }
    if overrides:
        values.update(overrides)
    return json.dumps(
        {
            "Parameters": [
                {"Name": f"/daily-insights/production/api/{key}", "Value": value}
                for key, value in values.items()
            ]
        }
    )


class MaterializationContractTest(unittest.TestCase):
    def load(self, payload: str) -> dict[str, str]:
        completed = subprocess.CompletedProcess([], 0, stdout=payload, stderr="")
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed):
            return dict(
                MODULE.load_parameters(
                    "ap-southeast-1", "/daily-insights/production/api"
                )
            )

    def test_loads_exact_required_keys(self) -> None:
        values = self.load(parameter_payload())
        self.assertEqual(set(values), MODULE.REQUIRED_API_KEYS)

    def test_rejects_missing_required_key(self) -> None:
        payload = json.loads(parameter_payload())
        payload["Parameters"].pop()
        with self.assertRaisesRegex(ValueError, "missing required parameter"):
            self.load(json.dumps(payload))

    def test_rejects_nested_or_env_unsafe_values(self) -> None:
        nested = json.loads(parameter_payload())
        nested["Parameters"][0]["Name"] = (
            "/daily-insights/production/api/nested/"
            f"{nested['Parameters'][0]['Name'].rsplit('/', 1)[-1]}"
        )
        with self.assertRaisesRegex(ValueError, "direct env-key child"):
            self.load(json.dumps(nested))

        with self.assertRaisesRegex(ValueError, "not safe for a Docker env file"):
            self.load(
                parameter_payload(
                    {"DAILY_INSIGHTS_SESSION_SECRET": "unsafe value with spaces"}
                )
            )

    def test_atomic_write_applies_requested_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "api.env"
            MODULE.atomic_write(target, "KEY=value\n", 0o600)
            self.assertEqual(target.read_text(encoding="utf-8"), "KEY=value\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
