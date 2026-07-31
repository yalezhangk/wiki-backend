from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol
from uuid import UUID, uuid4

from app.schemas.maintenance import (
    MaintenanceJobResponse,
    MaintenanceResultState,
    MaintenanceTaskKind,
    MaintenanceTrigger,
)
from app.time_utils import beijing_now

LOGGER = logging.getLogger(__name__)


class MaintenanceNotFoundError(RuntimeError):
    """请求的维护任务不存在。"""


class MaintenanceStorage(Protocol):
    def create_maintenance_job(
        self,
        *,
        task_kind: MaintenanceTaskKind,
        trigger: MaintenanceTrigger,
        options: dict[str, Any],
        workflow_id: UUID | None,
        depends_on_job_id: int | None,
        now: datetime,
    ) -> MaintenanceJobResponse:
        ...

    def claim_due_maintenance_job(self, *, now: datetime) -> MaintenanceJobResponse | None:
        ...

    def mark_maintenance_job_succeeded(
        self,
        *,
        job_id: int,
        result_state: MaintenanceResultState,
        result_summary: dict[str, Any],
        finished_at: datetime,
    ) -> None:
        ...

    def update_maintenance_job_progress(
        self, *, job_id: int, stage: str, progress_percent: int, updated_at: datetime
    ) -> None:
        ...

    def mark_maintenance_job_failed(self, *, job_id: int, error: str, finished_at: datetime) -> None:
        ...

    def recover_maintenance_jobs(self, *, now: datetime) -> None:
        ...

    def get_maintenance_job(self, job_id: int) -> MaintenanceJobResponse | None:
        ...

    def list_maintenance_jobs(
        self,
        *,
        limit: int,
        task_kind: MaintenanceTaskKind | None,
        workflow_id: UUID | None,
    ) -> list[MaintenanceJobResponse]:
        ...


@dataclass(frozen=True)
class MaintenanceTaskResult:
    result_summary: dict[str, Any] = field(default_factory=dict)
    result_state: MaintenanceResultState = "complete"


MaintenanceHandler = Callable[[MaintenanceJobResponse], MaintenanceTaskResult]


class MaintenanceService:
    """串行执行可审计的 Wiki 维护任务。"""

    def __init__(
        self,
        *,
        storage: MaintenanceStorage,
        handlers: dict[MaintenanceTaskKind, MaintenanceHandler] | None = None,
        start_worker: bool = True,
    ) -> None:
        self._storage = storage
        self._handlers = handlers or {}
        self._wake_event = threading.Event()
        self._worker: threading.Thread | None = None
        if start_worker:
            self._storage.recover_maintenance_jobs(now=self._beijing_now())
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="maintenance-worker",
                daemon=True,
            )
            self._worker.start()

    def create_job(
        self,
        *,
        task_kind: MaintenanceTaskKind,
        options: dict[str, Any],
        trigger: MaintenanceTrigger = "manual",
        workflow_id: UUID | None = None,
        depends_on_job_id: int | None = None,
    ) -> MaintenanceJobResponse:
        job = self._storage.create_maintenance_job(
            task_kind=task_kind,
            trigger=trigger,
            options=self._default_options(task_kind, options),
            workflow_id=workflow_id,
            depends_on_job_id=depends_on_job_id,
            now=self._beijing_now(),
        )
        self._wake_event.set()
        return job

    def create_quality_workflow(self, *, lint_options: dict[str, Any]) -> tuple[UUID, list[MaintenanceJobResponse]]:
        workflow_id = uuid4()
        health = self.create_job(
            task_kind="health",
            options={},
            trigger="workflow",
            workflow_id=workflow_id,
        )
        graph = self.create_job(
            task_kind="graph",
            options={"infer_relations": False, "save_report": True},
            trigger="workflow",
            workflow_id=workflow_id,
            depends_on_job_id=health.job_id,
        )
        lint = self.create_job(
            task_kind="lint",
            options=lint_options,
            trigger="workflow",
            workflow_id=workflow_id,
            depends_on_job_id=graph.job_id,
        )
        return workflow_id, [health, graph, lint]

    def get_job(self, job_id: int) -> MaintenanceJobResponse:
        job = self._storage.get_maintenance_job(job_id)
        if job is None:
            raise MaintenanceNotFoundError(job_id)
        return job

    def list_jobs(
        self,
        *,
        limit: int,
        task_kind: MaintenanceTaskKind | None = None,
        workflow_id: UUID | None = None,
    ) -> list[MaintenanceJobResponse]:
        return self._storage.list_maintenance_jobs(
            limit=min(max(limit, 1), 100),
            task_kind=task_kind,
            workflow_id=workflow_id,
        )

    def _worker_loop(self) -> None:
        while True:
            job = self._storage.claim_due_maintenance_job(now=self._beijing_now())
            if job is None:
                self._wake_event.wait(timeout=1.0)
                self._wake_event.clear()
                continue
            self._run_job(job)

    def _run_job(self, job: MaintenanceJobResponse) -> None:
        try:
            handler = self._handlers.get(job.task_kind)
            if handler is None:
                raise RuntimeError(f"maintenance task handler is unavailable: {job.task_kind}")
            result = handler(job)
            self._storage.mark_maintenance_job_succeeded(
                job_id=job.job_id,
                result_state=result.result_state,
                result_summary=result.result_summary,
                finished_at=self._beijing_now(),
            )
            LOGGER.info("Maintenance task completed job_id=%s task_kind=%s", job.job_id, job.task_kind)
        except Exception as exc:
            LOGGER.exception("Maintenance task failed job_id=%s task_kind=%s", job.job_id, job.task_kind)
            self._storage.mark_maintenance_job_failed(
                job_id=job.job_id,
                error=self._safe_error(exc),
                finished_at=self._beijing_now(),
            )

    @staticmethod
    def _default_options(task_kind: MaintenanceTaskKind, options: dict[str, Any]) -> dict[str, Any]:
        defaults: dict[MaintenanceTaskKind, dict[str, Any]] = {
            "health": {"save_report": True},
            "graph": {"infer_relations": True, "save_report": True},
            "lint": {"semantic_analysis": True, "semantic_mode": "delta", "selected_page_paths": []},
        }
        return {**defaults[task_kind], **options}

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        message = str(exc).replace("\n", " ").strip()
        return (message or exc.__class__.__name__)[:1000]

    @staticmethod
    def _beijing_now() -> datetime:
        return beijing_now()
