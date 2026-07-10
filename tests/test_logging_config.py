from __future__ import annotations

import logging
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.logging_config import (
    DEFAULT_LOG_PATH,
    FILE_LOGGER_NAMES,
    configure_logging,
    _enforce_log_directory_size,
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

    def test_configure_logging_writes_to_requested_log_file(self) -> None:
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
                dated_log_path = log_path.with_name(
                    f"{log_path.name}.{date.today().isoformat()}",
                )

                configure_logging(log_path)
                configure_logging(log_path)
                logging.getLogger("tests.logging_config").info("file logging enabled")
                logging.getLogger("uvicorn.access").info(
                    '127.0.0.1:38838 - "GET /api/chats HTTP/1.1" 200 OK',
                )

                for logger in loggers:
                    for handler in logger.handlers:
                        handler.flush()

                self.assertFalse(log_path.exists())
                self.assertTrue(dated_log_path.exists())
                log_content = dated_log_path.read_text(encoding="utf-8")
                self.assertIn("file logging enabled", log_content)
                self.assertIn("GET /api/chats HTTP/1.1", log_content)
                access_file_handlers = [
                    handler
                    for handler in logging.getLogger("uvicorn.access").handlers
                    if (
                        isinstance(handler, logging.FileHandler)
                        and Path(handler.baseFilename) == dated_log_path.resolve()
                    )
                ]
                self.assertEqual(len(access_file_handlers), 1)
            finally:
                for logger in loggers:
                    for handler in logger.handlers[:]:
                        handler.close()
                        logger.removeHandler(handler)
                    handlers, level = previous_state[logger.name]
                    for handler in handlers:
                        logger.addHandler(handler)
                    logger.setLevel(level)

    def test_log_directory_cleanup_removes_oldest_logs_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            oldest_log = log_dir / "wiki-backend.log.2026-07-08"
            newer_log = log_dir / "wiki-backend.log.2026-07-09"
            active_log = log_dir / "wiki-backend.log.2026-07-10"

            oldest_log.write_bytes(b"a" * 60)
            newer_log.write_bytes(b"b" * 50)
            active_log.write_bytes(b"c" * 10)
            os.utime(oldest_log, (1, 1))
            os.utime(newer_log, (2, 2))
            os.utime(active_log, (3, 3))

            _enforce_log_directory_size(
                log_dir=log_dir,
                base_name="wiki-backend.log",
                active_log_path=active_log,
                max_bytes=70,
            )

            self.assertFalse(oldest_log.exists())
            self.assertTrue(newer_log.exists())
            self.assertTrue(active_log.exists())


if __name__ == "__main__":
    unittest.main()
