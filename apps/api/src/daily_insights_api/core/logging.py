"""Process-wide logging setup for the API and the scheduler scripts.

`emit_event` writes one JSON object per line to the ``daily_insights`` logger.
Uvicorn only configures its own loggers, so without a handler here every
structured event was dropped by Python's last-resort WARNING handler in
production. The handler writes to stderr so `docker logs` shows the events.
"""

import logging
import sys

APP_LOGGER = "daily_insights"
_HANDLER_NAME = "daily-insights-stderr"


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Attach a single stderr handler to the application logger (idempotent)."""
    logger = logging.getLogger(APP_LOGGER)
    logger.setLevel(level)
    if not any(handler.get_name() == _HANDLER_NAME for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.set_name(_HANDLER_NAME)
        handler.setLevel(level)
        # Events are already JSON; plain messages keep them machine-readable.
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    # Propagation stays on: the root logger has no handlers in production, so
    # nothing is duplicated, and test log capture keeps working.
    return logger
