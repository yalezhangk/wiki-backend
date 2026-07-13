from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_PATH = (
    Path.home()
    / "Logs"
    / "knowledge_base_mkt"
    / "wiki-backend"
    / "wiki-backend.log"
)
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
FILE_LOGGER_NAMES = ("", "uvicorn", "uvicorn.access")
LOG_FILE_MAX_BYTES = 200 * 1024 * 1024
LOG_FILE_MAX_COUNT = 50
LOG_FILE_BACKUP_COUNT = LOG_FILE_MAX_COUNT - 1


def configure_logging(log_path: Path | None = None) -> None:
    target_log_path = (log_path or DEFAULT_LOG_PATH).expanduser().resolve()
    target_log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = _get_or_create_file_handler(target_log_path)

    for logger_name in FILE_LOGGER_NAMES:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        if file_handler not in logger.handlers:
            logger.addHandler(file_handler)


def _get_or_create_file_handler(log_path: Path) -> RotatingFileHandler:
    existing_handler = _find_file_handler(log_path)
    if existing_handler is not None:
        return existing_handler

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return file_handler


def _find_file_handler(log_path: Path) -> RotatingFileHandler | None:
    for logger_name in FILE_LOGGER_NAMES:
        for handler in logging.getLogger(logger_name).handlers:
            if (
                isinstance(handler, RotatingFileHandler)
                and Path(handler.baseFilename) == log_path
            ):
                return handler
    return None
