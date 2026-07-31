from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.maintenance import MaintenanceJobResponse
from app.services.maintenance_service import MaintenanceNotFoundError


class FakeMaintenanceService:
    def __init__(self) -> None:
        now = datetime(2026, 7, 29, 9)
        self.job = MaintenanceJobResponse(
            job_id=1,
            task_kind="health",
            status="queued",
            result_state="unavailable",
            trigger="manual",
            stage="queued",
            progress_percent=0,
            options={"save_report": True},
            result_summary={},
            created_at=now,
            updated_at=now,
        )

    def create_job(self, *, task_kind: str, options: dict[str, object]) -> MaintenanceJobResponse:
        self.job = self.job.model_copy(update={"task_kind": task_kind, "options": options})
        return self.job

    def list_jobs(self, **_: object) -> list[MaintenanceJobResponse]:
        return [self.job]

    def get_job(self, job_id: int) -> MaintenanceJobResponse:
        if job_id != self.job.job_id:
            raise MaintenanceNotFoundError(job_id)
        return self.job

    def create_quality_workflow(self, *, lint_options: dict[str, object]) -> tuple[UUID, list[MaintenanceJobResponse]]:
        workflow_id = uuid4()
        jobs = [
            self.job.model_copy(update={"job_id": 1, "task_kind": "health", "trigger": "workflow", "workflow_id": workflow_id}),
            self.job.model_copy(update={"job_id": 2, "task_kind": "graph", "trigger": "workflow", "workflow_id": workflow_id, "depends_on_job_id": 1}),
            self.job.model_copy(update={"job_id": 3, "task_kind": "lint", "trigger": "workflow", "workflow_id": workflow_id, "depends_on_job_id": 2, "options": lint_options}),
        ]
        return workflow_id, jobs


class MaintenanceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeMaintenanceService()
        self.client = TestClient(
            create_app(maintenance_service=self.service, initialize_storage=False)  # type: ignore[arg-type]
        )

    def test_create_job_returns_accepted(self) -> None:
        response = self.client.post("/api/maintenance/jobs", json={"task_kind": "health"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["task_kind"], "health")

    def test_unknown_option_is_rejected(self) -> None:
        response = self.client.post("/api/maintenance/jobs", json={"task_kind": "health", "options": {"unsafe": True}})

        self.assertEqual(response.status_code, 422)

    def test_agent_compat_and_graph_report_options_are_accepted(self) -> None:
        lint = self.client.post("/api/maintenance/jobs", json={"task_kind": "lint", "options": {"semantic_mode": "agent_compat"}})
        graph = self.client.post("/api/maintenance/jobs", json={"task_kind": "graph", "options": {"save_report": False}})

        self.assertEqual(lint.status_code, 202)
        self.assertEqual(graph.status_code, 202)

    def test_maintenance_boolean_options_are_validated(self) -> None:
        response = self.client.post("/api/maintenance/jobs", json={"task_kind": "graph", "options": {"save_report": "true"}})

        self.assertEqual(response.status_code, 422)

    def test_workflow_returns_three_dependent_jobs(self) -> None:
        response = self.client.post("/api/maintenance/workflows/quality", json={})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(response.json()["jobs"]), 3)
        self.assertEqual(response.json()["jobs"][2]["depends_on_job_id"], 2)

    def test_missing_job_returns_404(self) -> None:
        self.assertEqual(self.client.get("/api/maintenance/jobs/2").status_code, 404)

    def test_service_unavailable_returns_503(self) -> None:
        client = TestClient(create_app(initialize_storage=False))

        response = client.get("/api/maintenance/jobs")

        self.assertEqual(response.status_code, 503)

    def test_default_app_initializes_maintenance_service_after_storage_is_ready(self) -> None:
        maintenance_service = object()
        with (
            patch("app.main.storage.initialize"),
            patch("app.main.MaintenanceService", return_value=maintenance_service),
        ):
            app = create_app()
            with TestClient(app):
                self.assertIs(app.state.maintenance_service, maintenance_service)

    def test_openapi_documents_maintenance_options_and_polling(self) -> None:
        document = self.client.get("/openapi.json").json()
        create_job = document["paths"]["/api/maintenance/jobs"]["post"]
        schema = document["components"]["schemas"]["MaintenanceJobCreateRequest"]

        self.assertIn("轮询", create_job["description"])
        self.assertIn("共享 Wiki", create_job["description"])
        self.assertIn("selected_page_paths", create_job["description"])
        self.assertIn("save_report", schema["properties"]["options"]["description"])
        self.assertIn("selected_page_paths", schema["properties"]["options"]["description"])
        summary = document["components"]["schemas"]["MaintenanceJobResponse"]["properties"]["result_summary"]
        self.assertIn("SHA-256", summary["description"])


if __name__ == "__main__":
    unittest.main()
