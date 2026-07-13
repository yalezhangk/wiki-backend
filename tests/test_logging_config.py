from __future__ import annotations

import logging
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.logging_config import (
    DEFAULT_LOG_PATH,
    FILE_LOGGER_NAMES,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_MAX_BYTES,
    LOG_FILE_MAX_COUNT,
    configure_logging,
)


class LoggingConfigTests(unittest.TestCase):
    def test_default_log_path_uses_home_logs_directory(self) -> None:
        self.assertEqual(
            DEFAULT_LOG_PATH,
            Path.home()
            / "Logs"
            / "knowledge_base_mkt"
            / "wiki-backend"
            / "wiki-backend.log",
        )

    def test_configure_logging_uses_rotating_wiki_backend_log(self) -> None:
        loggers = [logging.getLogger(name) for name in FILE_LOGGER_NAMES]
        previous_state = {
            logger.name: (logger.handlers[:], logger.level)
            for logger in loggers
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                for logger in loggers:
                    for handler in logger.handlers[:]:
                        logger.removeHandler(handler)

                log_path = (
                    Path(temp_dir)
                    / "Logs"
                    / "knowledge_base_mkt"
                    / "wiki-backend"
                    / "wiki-backend.log"
                )

                configure_logging(log_path)
                configure_logging(log_path)
                logging.getLogger("tests.logging_config").info("file logging enabled")
                logging.getLogger("uvicorn.access").info(
                    '127.0.0.1:38838 - "GET /api/chats HTTP/1.1" 200 OK',
                )

                active_handlers = _unique_handlers(loggers)
                for handler in active_handlers:
                    handler.flush()

                self.assertTrue(log_path.exists())
                log_content = log_path.read_text(encoding="utf-8")
                self.assertIn("file logging enabled", log_content)
                self.assertIn("GET /api/chats HTTP/1.1", log_content)

                rotating_handlers = [
                    handler
                    for handler in active_handlers
                    if (
                        isinstance(handler, RotatingFileHandler)
                        and Path(handler.baseFilename) == log_path.resolve()
                    )
                ]
                self.assertEqual(len(rotating_handlers), 1)
                self.assertEqual(rotating_handlers[0].maxBytes, LOG_FILE_MAX_BYTES)
                self.assertEqual(
                    rotating_handlers[0].backupCount,
                    LOG_FILE_BACKUP_COUNT,
                )
                self.assertEqual(LOG_FILE_BACKUP_COUNT + 1, LOG_FILE_MAX_COUNT)
            finally:
                for handler in _unique_handlers(loggers):
                    handler.close()
                for logger in loggers:
                    for handler in logger.handlers[:]:
                        logger.removeHandler(handler)
                    handlers, level = previous_state[logger.name]
                    for handler in handlers:
                        logger.addHandler(handler)
                    logger.setLevel(level)


def _unique_handlers(loggers: list[logging.Logger]) -> list[logging.Handler]:
    handlers: list[logging.Handler] = []
    for logger in loggers:
        for handler in logger.handlers:
            if handler not in handlers:
                handlers.append(handler)
    return handlers


if __name__ == "__main__":
    unittest.main()
