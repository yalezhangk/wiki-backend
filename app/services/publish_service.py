from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from app.schemas.publish import PublicationResponse, PublishJobResponse, PublishStatusResponse

LOGGER = logging.getLogger(__name__)


class PublishNotFoundError(RuntimeError):
    """请求的发布任务不存在。"""


class PublishStorage(Protocol):
    def queue_publish_change(
        self,
        *,
        source_kind: str,
        source_id: str,
        scheduled_at: datetime,
        max_scheduled_at: datetime,
        now: datetime,
    ) -> PublishJobResponse:
        ...

    def request_manual_publish(self, *, now: datetime) -> PublishJobResponse:
        ...

    def claim_due_publish_job(self, *, now: datetime) -> PublishJobResponse | None:
        ...

    def mark_publish_job_succeeded(
        self, *, job_id: str, release_id: str, finished_at: datetime
    ) -> None:
        ...

    def mark_publish_job_failed(self, *, job_id: str, error: str, finished_at: datetime) -> None:
        ...

    def recover_publish_jobs(self, *, now: datetime) -> None:
        ...

    def get_publish_job(self, job_id: str) -> PublishJobResponse | None:
        ...

    def list_publish_jobs(self, limit: int) -> list[PublishJobResponse]:
        ...

    def get_publish_status(self) -> PublishStatusResponse:
        ...

    def get_publication(self, *, source_kind: str, source_id: str) -> PublicationResponse | None:
        ...


class PublishService:
    """串行构建 Quartz，并以原子链接替换当前静态版本。"""

    def __init__(
        self,
        *,
        storage: PublishStorage,
        wiki_repo_path: Path,
        quartz_repo_path: Path,
        node_executable: str,
        build_timeout_seconds: int,
        debounce_seconds: int,
        max_delay_seconds: int,
        wiki_lock: threading.RLock,
        start_worker: bool = True,
    ) -> None:
        self._storage = storage
        self._wiki_dir = wiki_repo_path.resolve() / "wiki"
        self._quartz_root = quartz_repo_path.resolve()
        self._node_executable = node_executable
        self._build_timeout_seconds = build_timeout_seconds
        self._debounce_seconds = debounce_seconds
        self._max_delay_seconds = max_delay_seconds
        self._wiki_lock = wiki_lock
        self._wake_event = threading.Event()
        self._worker: threading.Thread | None = None
        if start_worker:
            self._storage.recover_publish_jobs(now=self._utc_now())
            self._worker = threading.Thread(target=self._worker_loop, name="publish-worker", daemon=True)
            self._worker.start()

    def queue_change(self, *, source_kind: str, source_id: str) -> PublishJobResponse:
        now = self._utc_now()
        job = self._storage.queue_publish_change(
            source_kind=source_kind,
            source_id=source_id,
            scheduled_at=now + timedelta(seconds=self._debounce_seconds),
            max_scheduled_at=now + timedelta(seconds=self._max_delay_seconds),
            now=now,
        )
        self._wake_event.set()
        return job

    def request_manual_publish(self) -> PublishJobResponse:
        job = self._storage.request_manual_publish(now=self._utc_now())
        self._wake_event.set()
        return job

    def get_publication(self, *, source_kind: str, source_id: str) -> PublicationResponse | None:
        return self._storage.get_publication(source_kind=source_kind, source_id=source_id)

    def get_status(self) -> PublishStatusResponse:
        return self._storage.get_publish_status()

    def get_job(self, job_id: str) -> PublishJobResponse:
        job = self._storage.get_publish_job(job_id)
        if job is None:
            raise PublishNotFoundError(job_id)
        return job

    def list_jobs(self, limit: int) -> list[PublishJobResponse]:
        return self._storage.list_publish_jobs(min(max(limit, 1), 100))

    def _worker_loop(self) -> None:
        while True:
            job = self._storage.claim_due_publish_job(now=self._utc_now())
            if job is None:
                self._wake_event.wait(timeout=1.0)
                self._wake_event.clear()
                continue
            self._run_job(job)

    def _run_job(self, job: PublishJobResponse) -> None:
        try:
            self._validate_runtime_paths()
            snapshot_dir, release_dir = self._prepare_job_directories(job.job_id)
            with self._wiki_lock:
                shutil.copytree(self._wiki_dir, snapshot_dir, dirs_exist_ok=False)
            self._build(snapshot_dir=snapshot_dir, release_dir=release_dir)
            self._validate_release(release_dir)
            self._activate_release(release_dir)
            self._storage.mark_publish_job_succeeded(
                job_id=job.job_id,
                release_id=job.job_id,
                finished_at=self._utc_now(),
            )
            self._prune_releases()
            LOGGER.info("Quartz publish completed job_id=%s", job.job_id)
        except Exception as exc:
            LOGGER.exception("Quartz publish failed job_id=%s", job.job_id)
            self._storage.mark_publish_job_failed(
                job_id=job.job_id,
                error=self._safe_error(exc),
                finished_at=self._utc_now(),
            )

    def _validate_runtime_paths(self) -> None:
        if not self._wiki_dir.is_dir():
            raise RuntimeError("Wiki source directory is unavailable")
        if not (self._quartz_root / "quartz" / "bootstrap-cli.mjs").is_file():
            raise RuntimeError("Quartz build entrypoint is unavailable")

    def _prepare_job_directories(self, job_id: str) -> tuple[Path, Path]:
        root = self._quartz_root / ".publish"
        snapshot_dir = root / "work" / job_id / "wiki"
        release_dir = root / "releases" / job_id
        snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
        release_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(snapshot_dir.parent, ignore_errors=True)
        shutil.rmtree(release_dir, ignore_errors=True)
        return snapshot_dir, release_dir

    def _build(self, *, snapshot_dir: Path, release_dir: Path) -> None:
        environment = os.environ.copy()
        environment["CHAT_PROXY_URL"] = "/api"
        command = [
            self._node_executable,
            "quartz/bootstrap-cli.mjs",
            "build",
            "-d",
            str(snapshot_dir),
            "-o",
            str(release_dir),
        ]
        completed = subprocess.run(
            command,
            cwd=self._quartz_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._build_timeout_seconds,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"Quartz build exited with code {completed.returncode}: {detail[-800:]}")

    def _validate_release(self, release_dir: Path) -> None:
        expected = ["index.html", "ingest.html", "chats.html", "static/contentIndex.json"]
        missing = [name for name in expected if not (release_dir / name).is_file()]
        if missing:
            raise RuntimeError(f"Quartz build is missing required files: {', '.join(missing)}")
        for name in ("index.html", "ingest.html", "chats.html"):
            content = (release_dir / name).read_text(encoding="utf-8")
            if "/quartz/" in content:
                raise RuntimeError("Quartz build contains an invalid /quartz/ resource prefix")
        chats_html = (release_dir / "chats.html").read_text(encoding="utf-8")
        if 'data-proxy-url="/api"' not in chats_html:
            raise RuntimeError("Quartz build does not use the same-origin /api proxy")

    def _activate_release(self, release_dir: Path) -> None:
        public = self._quartz_root / "public"
        temporary = self._quartz_root / ".publish" / "public.next"
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        temporary.symlink_to(release_dir, target_is_directory=True)
        if public.exists() and not public.is_symlink():
            legacy = self._quartz_root / ".publish" / "legacy-public"
            if legacy.exists() or legacy.is_symlink():
                shutil.rmtree(legacy, ignore_errors=True)
            public.replace(legacy)
        temporary.replace(public)

    def _prune_releases(self) -> None:
        releases = self._quartz_root / ".publish" / "releases"
        if not releases.is_dir():
            return
        entries = sorted((entry for entry in releases.iterdir() if entry.is_dir()), key=lambda entry: entry.stat().st_mtime, reverse=True)
        for entry in entries[3:]:
            shutil.rmtree(entry, ignore_errors=True)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        message = str(exc).replace("\n", " ").strip()
        return (message or exc.__class__.__name__)[:1000]

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
