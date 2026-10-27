"""A chat provider for replays other than the one production's containers name.

A replay normally reads the LLM endpoint, model and key of the production API
container. To compare another provider serving the same model (OpenCode Go's
``deepseek-v4.1-flash`` against DeepSeek's ``deepseek-chat``) a run names the
endpoint, the model, a file holding the key and the extra request fields
itself. The key is read from that file only when a call has to go out, kept in
memory, and never written to settings, run metadata, ledgers or logs.

The provider's tag (endpoint and extra fields) is part of every cache key, so
answers with thinking on and off, or from two providers, never mix.

Editor calls (selection, duplicate and repeat screens) can carry their own
extra fields and timeout, as ``newsroom_editor_extra_body`` and
``newsroom_editor_timeout_seconds`` let production do; the same reserved-field
and timeout rules as the settings apply.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from daily_insights_api.core.config import NEWSROOM_LLM_RESERVED_BODY_FIELDS
from daily_insights_api.modules.newsroom.replay.snapshot import ReplayError

EVAL_USER_AGENT = "daily-insights-newsroom-eval/1.0"
CALL_STAGES = ("triage", "editor")
# The longest per-call timeout the settings accept (``newsroom_llm_timeout_seconds``).
MAX_TIMEOUT_SECONDS = 300.0


def check_request_fields(name: str, body: Mapping[str, Any]) -> None:
    """Refuse extra fields that would replace the client's own (model, messages, ...)."""
    reserved = sorted(NEWSROOM_LLM_RESERVED_BODY_FIELDS & body.keys())
    if reserved:
        raise ValueError(f"{name} cannot set {', '.join(reserved)}")


def check_timeout(name: str, seconds: float | None) -> None:
    if seconds is not None and not 0 < seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"{name} must be above 0 and at most {MAX_TIMEOUT_SECONDS:g} seconds")


def parse_extra_body(text: str) -> dict[str, Any]:
    """A JSON object of extra request fields; the client's own fields are refused."""
    try:
        value = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as error:
        raise ValueError(f"extra body is not JSON: {error.msg}") from error
    if not isinstance(value, dict):
        raise ValueError("extra body must be a JSON object")
    check_request_fields("extra body", value)
    return value


@dataclass(frozen=True, slots=True)
class LlmProvider:
    base_url: str
    model: str
    key_file: Path
    extra_body: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    user_agent: str = EVAL_USER_AGENT
    # Header carrying the client's stable per-stage session id (``providers.session_id``).
    session_header: str | None = None
    # Call stages sent here (``editor`` covers selection, duplicate and repeat screens);
    # the others are answered from the production provider's cache only.
    stages: tuple[str, ...] = CALL_STAGES
    timeout_seconds: float = 90.0
    # Merged over ``extra_body`` for editor calls only (top-level keys replace).
    editor_extra_body: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    # None: editor calls use ``timeout_seconds``.
    editor_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        check_request_fields("extra body", self.extra_body)
        check_request_fields("editor extra body", self.editor_extra_body)
        check_timeout("timeout", self.timeout_seconds)
        check_timeout("editor timeout", self.editor_timeout_seconds)

    def cache_tag(self) -> str:
        """What tells this provider's answers apart from another's for the same request.

        Editor extra fields join the tag only when set, so tags (and cached
        answers) from before they existed stay valid.
        """
        tag: dict[str, Any] = {
            "base_url": self.base_url.rstrip("/"),
            "extra_body": dict(self.extra_body),
        }
        if self.editor_extra_body:
            tag["editor_extra_body"] = dict(self.editor_extra_body)
        return json.dumps(tag, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def read_key(self) -> str:
        try:
            key = self.key_file.expanduser().read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ReplayError(f"cannot read the key file {self.key_file}") from error
        if not key:
            raise ReplayError(f"the key file {self.key_file} is empty")
        return key

    def describe(self) -> dict[str, Any]:
        """The run metadata entry: everything but the key and where it lives."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "extra_body": dict(self.extra_body),
            "user_agent": self.user_agent,
            "session_header": self.session_header,
            "stages": list(self.stages),
            "timeout_seconds": self.timeout_seconds,
            "editor_extra_body": dict(self.editor_extra_body),
            "editor_timeout_seconds": self.editor_timeout_seconds,
        }
