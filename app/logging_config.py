from __future__ import annotations

import logging
from datetime import date
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
LOG_DIRECTORY_MAX_BYTES = 5 * 1024 * 1024 * 1024


def configure_logging(log_path: Path | None = None) -> None:
    base_log_path = (log_path or DEFAULT_LOG_PATH).expanduser().resolve()
    base_log_path.parent.mkdir(parents=True, exist_ok=True)
    _enforce_log_directory_size(
        log_dir=base_log_path.parent,
        base_name=base_log_path.name,
        active_log_path=_dated_log_path(base_log_path),
        max_bytes=LOG_DIRECTORY_MAX_BYTES,
    )

    for logger_name in FILE_LOGGER_NAMES:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        _ensure_file_handler(logger, base_log_path)


def _ensure_file_handler(logger: logging.Logger, base_log_path: Path) -> None:
    for handler in logger.handlers:
        if (
            isinstance(handler, DatedCappedFileHandler)
            and handler.base_log_path == base_log_path
        ):
            return

    file_handler = DatedCappedFileHandler(
        base_log_path=base_log_path,
        max_bytes=LOG_DIRECTORY_MAX_BYTES,
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(file_handler)


class DatedCappedFileHandler(logging.FileHandler):
    def __init__(self, *, base_log_path: Path, max_bytes: int) -> None:
        self.base_log_path = base_log_path
        self.max_bytes = max_bytes
        self.current_log_date = date.today()
        super().__init__(
            _dated_log_path(base_log_path, self.current_log_date),
            encoding="utf-8",
        )

    def emit(self, record: logging.LogRecord) -> None:
        self._roll_to_current_date()
        super().emit(record)
        self.flush()
        try:
            _enforce_log_directory_size(
                log_dir=self.base_log_path.parent,
                base_name=self.base_log_path.name,
                active_log_path=Path(self.baseFilename),
                max_bytes=self.max_bytes,
            )
        except OSError:
            self.handleError(record)

    def _roll_to_current_date(self) -> None:
        today = date.today()
        if today == self.current_log_date:
            return

        self.acquire()
        try:
            self.current_log_date = today
            self.baseFilename = str(_dated_log_path(self.base_log_path, today))
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self.stream = self._open()
        finally:
            self.release()


def _dated_log_path(base_log_path: Path, log_date: date | None = None) -> Path:
    target_date = log_date or date.today()
    return base_log_path.with_name(
        f"{base_log_path.name}.{target_date.isoformat()}",
    )


def _enforce_log_directory_size(
    *,
    log_dir: Path,
    base_name: str,
    active_log_path: Path,
    max_bytes: int,
) -> None:
    log_files = _list_log_files(log_dir, base_name)
    total_size = sum(log_file.stat().st_size for log_file in log_files)
    if total_size <= max_bytes:
        return

    for log_file in sorted(log_files, key=lambda path: path.stat().st_mtime):
        if log_file == active_log_path:
            continue
        file_size = log_file.stat().st_size
        log_file.unlink()
        total_size -= file_size
        if total_size <= max_bytes:
            return


def _list_log_files(log_dir: Path, base_name: str) -> list[Path]:
    return [
        path
        for path in log_dir.iterdir()
        if path.is_file() and path.name.startswith(base_name)
    ]
