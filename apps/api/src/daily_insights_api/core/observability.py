import json
import logging
from collections.abc import Mapping

SENSITIVE_FIELD_FRAGMENTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "access_key",
)

logger = logging.getLogger("daily_insights")


def sanitize_fields(fields: Mapping[str, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for key, value in fields.items():
        normalized = key.lower().replace("-", "_")
        if any(fragment in normalized for fragment in SENSITIVE_FIELD_FRAGMENTS):
            sanitized[key] = "[REDACTED]"
        elif isinstance(value, Mapping):
            sanitized[key] = sanitize_fields(value)
        else:
            sanitized[key] = value
    return sanitized


def emit_event(event: str, **fields: object) -> None:
    payload = {"event": event, **sanitize_fields(fields)}
    logger.info(json.dumps(payload, separators=(",", ":"), default=str))
