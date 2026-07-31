from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch
from uuid import UUID

from app.schemas.maintenance import MaintenanceJobResponse
from app.services.maintenance_service import MaintenanceService, MaintenanceTaskResult
from app.storage.mysql import MySQLStorage


class FakeStorage:
    def __init__(self) -> None:
        self.jobs: dict[int, MaintenanceJobResponse] = {}
        self.recovered = False
        self._next_id = 1

    def create_maintenance_job(self, **kwargs: object) -> MaintenanceJobResponse:
        now = kwargs["now"]
        assert isinstance(now, datetime)
        job = MaintenanceJobResponse(
            job_id=self._next_id,
            task_kind=kwargs["task_kind"],  # type: ignore[arg-type]
            status="queued",
            result_state="unavailable",
            trigger=kwargs["trigger"],  # type: ignore[arg-type]
            workflow_id=kwargs["workflow_id"],  # type: ignore[arg-type]
            depends_on_job_id=kwargs["depends_on_job_id"],  # type: ignore[arg-type]
            stage="queued",
            progress_percent=0,
            options=kwargs["options"],  # type: ignore[arg-type]
            result_summary={},
            created_at=now,
            updated_at=now,
        )
        self.jobs[job.job_id] = job
        self._next_id += 1
        return job

    def claim_due_maintenance_job(self, *, now: datetime) -> MaintenanceJobResponse | None:
        for job_id, job in self.jobs.items():
            dependency = self.jobs.get(job.depends_on_job_id) if job.depends_on_job_id else None
            if job.status == "queued" and dependency is not None and dependency.status == "failed":
                self.jobs[job_id] = job.model_copy(
                    update={
                        "status": "failed",
                        "stage": "dependency_failed",
                        "error": "dependency job failed",
                        "finished_at": now,
                        "updated_at": now,
                    }
                )
        for job_id, job in self.jobs.items():
            dependency = self.jobs.get(job.depends_on_job_id) if job.depends_on_job_id else None
            if job.status == "queued" and (dependency is None or dependency.status == "succeeded"):
                claimed = job.model_copy(
                    update={"status": "running", "stage": "starting", "progress_percent": 5, "started_at": now, "updated_at": now}
                )
                self.jobs[job_id] = claimed
                return claimed
        return None

    def mark_maintenance_job_succeeded(self, **kwargs: object) -> None:
        job_id = kwargs["job_id"]
        finished_at = kwargs["finished_at"]
        assert isinstance(job_id, int)
        assert isinstance(finished_at, datetime)
        self.jobs[job_id] = self.jobs[job_id].model_copy(
            update={
                "status": "succeeded",
                "result_state": kwargs["result_state"],
                "result_summary": kwargs["result_summary"],
                "stage": "completed",
                "progress_percent": 100,
                "finished_at": finished_at,
                "updated_at": finished_at,
            }
        )

    def mark_maintenance_job_failed(self, **kwargs: object) -> None:
        job_id = kwargs["job_id"]
        finished_at = kwargs["finished_at"]
        assert isinstance(job_id, int)
        assert isinstance(finished_at, datetime)
        self.jobs[job_id] = self.jobs[job_id].model_copy(
            update={
                "status": "failed",
                "stage": "failed",
                "error": kwargs["error"],
                "finished_at": finished_at,
                "updated_at": finished_at,
            }
        )

    def recover_maintenance_jobs(self, *, now: datetime) -> None:
        self.recovered = True
        for job_id, job in self.jobs.items():
            if job.status == "running":
                self.jobs[job_id] = job.model_copy(
                    update={"status": "failed", "stage": "failed", "error": "maintenance worker restarted", "finished_at": now, "updated_at": now}
                )

    def get_maintenance_job(self, job_id: int) -> MaintenanceJobResponse | None:
        return self.jobs.get(job_id)

    def list_maintenance_jobs(
        self, *, limit: int, task_kind: str | None, workflow_id: UUID | None
    ) -> list[MaintenanceJobResponse]:
        jobs = list(self.jobs.values())
        if task_kind is not None:
            jobs = [job for job in jobs if job.task_kind == task_kind]
        if workflow_id is not None:
            jobs = [job for job in jobs if job.workflow_id == workflow_id]
        return jobs[:limit]


class MaintenanceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = FakeStorage()

    def test_quality_workflow_uses_dependencies_and_default_options(self) -> None:
        service = MaintenanceService(storage=self.storage, start_worker=False)

        workflow_id, jobs = service.create_quality_workflow(lint_options={})

        self.assertEqual(len(jobs), 3)
        self.assertEqual([job.task_kind for job in jobs], ["health", "graph", "lint"])
        self.assertEqual(jobs[1].depends_on_job_id, jobs[0].job_id)
        self.assertEqual(jobs[2].depends_on_job_id, jobs[1].job_id)
        self.assertEqual(jobs[0].options, {"save_report": True})
        self.assertEqual(jobs[1].options, {"infer_relations": True, "save_report": True})
        self.assertEqual(
            jobs[2].options,
            {"semantic_analysis": True, "semantic_mode": "delta", "selected_page_paths": []},
        )
        self.assertEqual(jobs[2].workflow_id, workflow_id)

    def test_direct_graph_job_enables_relation_inference_by_default(self) -> None:
        service = MaintenanceService(storage=self.storage, start_worker=False)

        job = service.create_job(task_kind="graph", options={})

        self.assertTrue(job.options["infer_relations"])

    def test_worker_marks_partial_handler_result_as_succeeded(self) -> None:
        def handler(_: MaintenanceJobResponse) -> MaintenanceTaskResult:
            return MaintenanceTaskResult(result_state="partial", result_summary={"graph": "available"})

        service = MaintenanceService(storage=self.storage, handlers={"graph": handler}, start_worker=False)
        job = service.create_job(task_kind="graph", options={})
        claimed = self.storage.claim_due_maintenance_job(now=job.created_at)
        assert claimed is not None

        service._run_job(claimed)

        stored = self.storage.get_maintenance_job(job.job_id)
        assert stored is not None
        self.assertEqual(stored.status, "succeeded")
        self.assertEqual(stored.result_state, "partial")
        self.assertEqual(stored.result_summary, {"graph": "available"})

    def test_failed_dependency_is_not_claimed(self) -> None:
        service = MaintenanceService(storage=self.storage, start_worker=False)
        parent = service.create_job(task_kind="health", options={})
        child = service.create_job(task_kind="graph", options={}, depends_on_job_id=parent.job_id)
        self.storage.mark_maintenance_job_failed(
            job_id=parent.job_id,
            error="health failed",
            finished_at=parent.created_at,
        )

        self.assertIsNone(self.storage.claim_due_maintenance_job(now=parent.created_at))
        stored = self.storage.get_maintenance_job(child.job_id)
        assert stored is not None
        self.assertEqual(stored.stage, "dependency_failed")
        self.assertEqual(stored.error, "dependency job failed")

    def test_startup_recovery_marks_running_job_failed(self) -> None:
        service = MaintenanceService(storage=self.storage, start_worker=False)
        job = service.create_job(task_kind="health", options={})
        claimed = self.storage.claim_due_maintenance_job(now=job.created_at)
        assert claimed is not None

        MaintenanceService(storage=self.storage, start_worker=True)

        recovered = self.storage.get_maintenance_job(job.job_id)
        assert recovered is not None
        self.assertTrue(self.storage.recovered)
        self.assertEqual(recovered.error, "maintenance worker restarted")

    def test_mark_succeeded_binds_result_state(self) -> None:
        storage = MySQLStorage("127.0.0.1", 3306, "user", "password", "database")
        finished_at = datetime(2026, 7, 29, 9)
        with patch.object(storage, "_execute_update") as execute_update:
            storage.mark_maintenance_job_succeeded(
                job_id=1,
                result_state="partial",
                result_summary={"graph": "available"},
                finished_at=finished_at,
            )

        _, params = execute_update.call_args.args
        self.assertEqual(params[0], "partial")
        self.assertEqual(len(params), 5)


if __name__ == "__main__":
    unittest.main()
