import logging

import pytest

from daily_insights_api.core.logging import APP_LOGGER, configure_logging
from daily_insights_api.core.observability import emit_event


def test_configure_logging_attaches_one_stderr_handler_and_emits_events(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = logging.getLogger(APP_LOGGER)
    # Another test may already have configured the app (create_app); start clean
    # so the handler binds to the captured stderr.
    for handler in list(logger.handlers):
        if handler.get_name() == "daily-insights-stderr":
            logger.removeHandler(handler)
    original = list(logger.handlers)
    try:
        configure_logging()
        configure_logging()
        added = [handler for handler in logger.handlers if handler not in original]
        assert len(added) == 1
        assert logger.propagate is True
        emit_event("news.feed.stale", hostname="example.test", age_hours=30.5)
        captured = capsys.readouterr()
        assert (
            '{"event":"news.feed.stale","hostname":"example.test","age_hours":30.5}' in captured.err
        )
    finally:
        for handler in logger.handlers:
            if handler not in original:
                logger.removeHandler(handler)
