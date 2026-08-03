from __future__ import annotations

import logging

from app.config import settings
from app.logging_config import LOG_FORMAT, configure_logging
from app.services.scheduled_ingest_service import (
    LoopbackIngestApiClient,
    ScheduledIngestError,
    ScheduledIngestService,
)
from app.storage.mysql import storage

JOURNAL_HANDLER_NAME = "scheduled-ingest-journal"


def configure_journal_logging() -> None:
    root_logger = logging.getLogger()
    if any(handler.get_name() == JOURNAL_HANDLER_NAME for handler in root_logger.handlers):
        return

    handler = logging.StreamHandler()
    handler.set_name(JOURNAL_HANDLER_NAME)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root_logger.addHandler(handler)


def main() -> int:
    configure_logging()
    configure_journal_logging()
    logger = logging.getLogger(__name__)
    if settings.scheduled_ingest_root is None:
        logger.error("WIKI_BACKEND_SCHEDULED_INGEST_ROOT is not configured")
        return 2

    try:
        storage.initialize()
        service = ScheduledIngestService(
            storage=storage,
            api_client=LoopbackIngestApiClient(base_url=settings.scheduled_ingest_api_url),
            source_root=settings.scheduled_ingest_root,
            poll_seconds=settings.scheduled_ingest_poll_seconds,
            poll_timeout_seconds=settings.scheduled_ingest_poll_timeout_seconds,
        )
        summary = service.run()
    except ScheduledIngestError as exc:
        logger.error("Scheduled ingest could not run: %s", exc)
        return 2
    except Exception:
        logger.exception("Scheduled ingest crashed")
        return 2

    return 1 if summary.failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
