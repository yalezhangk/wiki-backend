from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.schemas.ingest import IngestJobResponse
from app.storage.mysql import ScheduledIngestSource
from app.time_utils import beijing_now

LOGGER = logging.getLogger(__name__)
UPLOAD_CHUNK_BYTES = 64 * 1024
SOURCE_URL_PATTERN = re.compile(r"(?im)^Source URL:\s*(https?://\S+)\s*$")


class ScheduledIngestError(RuntimeError):
    """定时源目录无法安全扫描或本机 Ingest API 不可用时抛出。"""


class ScheduledIngestDuplicateError(ScheduledIngestError):
    """定时来源已由全局文档名约束占用。"""


class ScheduledIngestStorage(Protocol):
    def recover_scheduled_ingest_sources(self, *, now: datetime) -> list[str]:
        ...

    def claim_scheduled_ingest_source(
        self,
        *,
        source_root: str,
        relative_path: str,
        source_device: int,
        source_inode: int,
        now: datetime,
    ) -> ScheduledIngestSource | None:
        ...

    def record_scheduled_ingest_attempt(
        self,
        *,
        source_id: int,
        ingest_job_id: int | None,
        attempted_at: datetime,
    ) -> None:
        ...

    def set_scheduled_ingest_job(self, *, source_id: int, ingest_job_id: int) -> None:
        ...

    def complete_scheduled_ingest_source(
        self,
        *,
        source_id: int,
        state: str,
        error: str | None,
        finished_at: datetime,
    ) -> None:
        ...


class ScheduledIngestApiClient(Protocol):
    def create_scheduled_job(
        self, *, source_path: Path, original_filename: str, source_url: str
    ) -> IngestJobResponse:
        ...

    def get_job(self, job_id: int) -> IngestJobResponse:
        ...


@dataclass(frozen=True)
class ScheduledIngestSummary:
    scanned_count: int = 0
    candidate_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    deferred_count: int = 0
    skipped_count: int = 0


@dataclass(frozen=True)
class StableSourceSnapshot:
    path: Path
    source_device: int
    source_inode: int


class LoopbackIngestApiClient:
    """仅调用本机 FastAPI Ingest worker 的最小 HTTP 客户端。"""

    def __init__(self, *, base_url: str, timeout_seconds: int = 60) -> None:
        self._base_url = self._validate_base_url(base_url)
        self._timeout_seconds = timeout_seconds

    def create_scheduled_job(
        self, *, source_path: Path, original_filename: str, source_url: str
    ) -> IngestJobResponse:
        boundary = f"----wiki-backend-{uuid.uuid4().hex}"
        payload = self._multipart_payload(
            boundary=boundary,
            source_path=source_path,
            original_filename=original_filename,
            source_url=source_url,
        )
        request = Request(
            url=f"{self._base_url}/api/ingest/jobs",
            data=payload,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(payload)),
            },
            method="POST",
        )
        return self._request_job(request)

    def get_job(self, job_id: int) -> IngestJobResponse:
        request = Request(
            url=f"{self._base_url}/api/ingest/jobs/{job_id}",
            method="GET",
        )
        return self._request_job(request)

    def _request_job(self, request: Request) -> IngestJobResponse:
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 409:
                raise ScheduledIngestDuplicateError("文档名称已存在，跳过重复定时入库") from exc
            raise ScheduledIngestError(f"loopback ingest API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, ValueError) as exc:
            raise ScheduledIngestError("loopback ingest API is unavailable") from exc
        try:
            return IngestJobResponse.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise ScheduledIngestError("loopback ingest API returned an invalid job response") from exc

    @staticmethod
    def _multipart_payload(
        *, boundary: str, source_path: Path, original_filename: str, source_url: str
    ) -> bytes:
        content = source_path.read_bytes()
        escaped_filename = original_filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        prefix = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="trigger"\r\n\r\n'
            "scheduled\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="auto_convert"\r\n\r\n'
            "true\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="source_url"\r\n\r\n'
            f"{source_url}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{escaped_filename}"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        return prefix + content + suffix

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in {"", "/"}:
            raise ScheduledIngestError(
                "WIKI_BACKEND_SCHEDULED_INGEST_API_URL must use http://127.0.0.1"
            )
        return base_url.rstrip("/")


class ScheduledIngestService:
    def __init__(
        self,
        *,
        storage: ScheduledIngestStorage,
        api_client: ScheduledIngestApiClient,
        source_root: Path,
        poll_seconds: float,
        poll_timeout_seconds: int,
        now: Callable[[], datetime] = beijing_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._storage = storage
        self._api_client = api_client
        self._source_root = source_root
        self._poll_seconds = poll_seconds
        self._poll_timeout_seconds = poll_timeout_seconds
        self._now = now
        self._sleep = sleep

    def run(self) -> ScheduledIngestSummary:
        root = self._validated_root()
        root_key = str(root)
        for recovery_error in self._storage.recover_scheduled_ingest_sources(now=self._now()):
            LOGGER.error("Scheduled ingest recovery failed source=%s", recovery_error)
        candidates = self._markdown_files(root)
        directory_counts = self._markdown_directory_counts(candidates)
        summary = ScheduledIngestSummary(scanned_count=len(candidates))
        LOGGER.info("Scheduled Markdown ingest scan started scanned=%s", summary.scanned_count)

        for source_path in candidates:
            relative_path = source_path.relative_to(root).as_posix()
            source_url, source_error = self._extract_source_url(
                directory=source_path.parent,
                markdown_count=directory_counts[source_path.parent],
            )
            if source_error is not None:
                summary = self._replace_summary(summary, failed_count=summary.failed_count + 1)
                LOGGER.error(
                    "Scheduled ingest skipped relative_path=%s reason=%s",
                    relative_path,
                    source_error,
                )
                continue
            assert source_url is not None
            with TemporaryDirectory(prefix="wiki-backend-scheduled-ingest-") as temporary_directory:
                snapshot = self._create_stable_snapshot(
                    source_path=source_path,
                    temporary_directory=Path(temporary_directory),
                )
                if snapshot is None:
                    summary = self._replace_summary(summary, deferred_count=summary.deferred_count + 1)
                    LOGGER.info(
                        "Scheduled ingest deferred relative_path=%s reason=source_changed_or_empty",
                        relative_path,
                    )
                    continue

                record = self._storage.claim_scheduled_ingest_source(
                    source_root=root_key,
                    relative_path=relative_path,
                    source_device=snapshot.source_device,
                    source_inode=snapshot.source_inode,
                    now=self._now(),
                )
                if record is None:
                    summary = self._replace_summary(summary, skipped_count=summary.skipped_count + 1)
                    LOGGER.info(
                        "Scheduled ingest skipped relative_path=%s reason=already_recorded",
                        relative_path,
                    )
                    continue

                summary = self._replace_summary(summary, candidate_count=summary.candidate_count + 1)
                LOGGER.info("Scheduled ingest processing relative_path=%s", relative_path)
                outcome = self._ingest_source(
                    record=record,
                    snapshot_path=snapshot.path,
                    original_filename=source_path.name,
                    source_url=source_url,
                )
                if outcome == "succeeded":
                    summary = self._replace_summary(summary, succeeded_count=summary.succeeded_count + 1)
                elif outcome == "duplicate":
                    summary = self._replace_summary(summary, skipped_count=summary.skipped_count + 1)
                else:
                    summary = self._replace_summary(summary, failed_count=summary.failed_count + 1)

        LOGGER.info(
            "Scheduled Markdown ingest finished scanned=%s candidates=%s succeeded=%s failed=%s deferred=%s skipped=%s",
            summary.scanned_count,
            summary.candidate_count,
            summary.succeeded_count,
            summary.failed_count,
            summary.deferred_count,
            summary.skipped_count,
        )
        return summary

    def _ingest_source(
        self,
        *,
        record: ScheduledIngestSource,
        snapshot_path: Path,
        original_filename: str,
        source_url: str,
    ) -> str:
        last_error: str | None = None
        job_id: int | None = None
        self._storage.record_scheduled_ingest_attempt(
            source_id=record.source_id,
            ingest_job_id=None,
            attempted_at=self._now(),
        )
        try:
            LOGGER.info("Scheduled ingest submitting relative_path=%s", record.relative_path)
            job = self._api_client.create_scheduled_job(
                source_path=snapshot_path,
                original_filename=original_filename,
                source_url=source_url,
            )
            job_id = job.job_id
            LOGGER.info(
                "Scheduled ingest accepted relative_path=%s job_id=%s",
                record.relative_path,
                job_id,
            )
            self._storage.set_scheduled_ingest_job(
                source_id=record.source_id,
                ingest_job_id=job_id,
            )
            completed_job = self._wait_for_terminal_job(job_id)
            if completed_job.status == "succeeded":
                self._storage.complete_scheduled_ingest_source(
                    source_id=record.source_id,
                    state="succeeded",
                    error=None,
                    finished_at=self._now(),
                )
                LOGGER.info(
                    "Scheduled ingest succeeded relative_path=%s job_id=%s",
                    record.relative_path,
                    job_id,
                )
                return "succeeded"
            last_error = completed_job.error or "ingest job failed"
            LOGGER.warning(
                "Scheduled ingest job failed relative_path=%s job_id=%s error=%s",
                record.relative_path,
                job_id,
                last_error,
            )
        except ScheduledIngestDuplicateError as exc:
            self._storage.complete_scheduled_ingest_source(
                source_id=record.source_id,
                state="skipped",
                error=str(exc)[:1000],
                finished_at=self._now(),
            )
            LOGGER.info(
                "Scheduled ingest duplicate skipped relative_path=%s",
                record.relative_path,
            )
            return "duplicate"
        except Exception as exc:
            last_error = str(exc)
            LOGGER.warning(
                "Scheduled ingest attempt failed relative_path=%s job_id=%s error=%s",
                record.relative_path,
                job_id,
                last_error,
            )

        safe_error = (last_error or "scheduled ingest failed")[:1000]
        self._storage.complete_scheduled_ingest_source(
            source_id=record.source_id,
            state="failed",
            error=safe_error,
            finished_at=self._now(),
        )
        LOGGER.error("Scheduled ingest failed relative_path=%s error=%s", record.relative_path, safe_error)
        return "failed"

    def _wait_for_terminal_job(self, job_id: int) -> IngestJobResponse:
        deadline = time.monotonic() + self._poll_timeout_seconds
        while True:
            job = self._api_client.get_job(job_id)
            if job.status in {"succeeded", "failed"}:
                return job
            if time.monotonic() >= deadline:
                raise ScheduledIngestError(f"ingest job polling timed out: {job_id}")
            self._sleep(self._poll_seconds)

    def _validated_root(self) -> Path:
        root = self._source_root.expanduser()
        if root.is_symlink() or not root.is_dir():
            raise ScheduledIngestError("WIKI_BACKEND_SCHEDULED_INGEST_ROOT must be a regular directory")
        return root.resolve()

    @staticmethod
    def _markdown_files(root: Path) -> list[Path]:
        files: list[Path] = []
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            current_directory = Path(directory)
            directory_names[:] = [
                name for name in directory_names if not (current_directory / name).is_symlink()
            ]
            for name in file_names:
                path = current_directory / name
                if path.is_symlink() or path.suffix.lower() != ".md" or not path.is_file():
                    continue
                files.append(path)
        return sorted(files, key=lambda path: path.relative_to(root).as_posix())

    @staticmethod
    def _markdown_directory_counts(candidates: list[Path]) -> dict[Path, int]:
        counts: dict[Path, int] = {}
        for candidate in candidates:
            counts[candidate.parent] = counts.get(candidate.parent, 0) + 1
        return counts

    @staticmethod
    def _extract_source_url(*, directory: Path, markdown_count: int) -> tuple[str | None, str | None]:
        if markdown_count != 1:
            return None, "multiple_markdown_files_in_directory"
        readme_path = directory / "readme.txt"
        try:
            content = readme_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return None, "source_url_readme_missing"
        except UnicodeDecodeError:
            return None, "source_url_readme_not_utf8"
        values = re.findall(r"(?im)^Source URL:\s*(\S+)\s*$", content)
        if not values:
            return None, "source_url_missing"
        if len(values) != 1:
            return None, "source_url_multiple"
        source_url_match = SOURCE_URL_PATTERN.search(content)
        if source_url_match is None:
            return None, "source_url_invalid_protocol"
        return source_url_match.group(1), None

    @staticmethod
    def _create_stable_snapshot(
        *, source_path: Path, temporary_directory: Path
    ) -> StableSourceSnapshot | None:
        if source_path.is_symlink() or not source_path.is_file():
            return None
        before = source_path.stat()
        with NamedTemporaryFile(
            mode="wb",
            suffix=".md",
            dir=temporary_directory,
            delete=False,
        ) as snapshot_file:
            snapshot_path = Path(snapshot_file.name)
            with source_path.open("rb") as source_file:
                shutil.copyfileobj(source_file, snapshot_file, length=UPLOAD_CHUNK_BYTES)
        after = source_path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            snapshot_path.unlink(missing_ok=True)
            return None
        return StableSourceSnapshot(
            path=snapshot_path,
            source_device=after.st_dev,
            source_inode=after.st_ino,
        )

    @staticmethod
    def _replace_summary(summary: ScheduledIngestSummary, **updates: int) -> ScheduledIngestSummary:
        values = summary.__dict__ | updates
        return ScheduledIngestSummary(**values)
