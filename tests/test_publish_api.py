from __future__ import annotations

import unittest
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.publish import PublishJobResponse, PublishStatusResponse
from app.services.publish_service import PublishNotFoundError


class FakePublishService:
    def __init__(self) -> None:
        self.job = PublishJobResponse(
            job_id="publish-1",
            status="queued",
            trigger="automatic",
            change_count=2,
            scheduled_at=datetime(2026, 7, 27, 9, 2),
            created_at=datetime(2026, 7, 27, 9),
            updated_at=datetime(2026, 7, 27, 9),
        )

    def get_status(self) -> PublishStatusResponse:
        return PublishStatusResponse(pending_change_count=2, active_job=self.job, last_successful_job=None)

    def request_manual_publish(self) -> PublishJobResponse:
        self.job = self.job.model_copy(update={"trigger": "manual", "scheduled_at": datetime(2026, 7, 27, 9)})
        return self.job

    def list_jobs(self, limit: int) -> list[PublishJobResponse]:
        return [self.job][:limit]

    def get_job(self, job_id: str) -> PublishJobResponse:
        if job_id != self.job.job_id:
            raise PublishNotFoundError(job_id)
        return self.job


class PublishApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakePublishService()
        self.client = TestClient(
            create_app(
                publish_service=self.service,  # type: ignore[arg-type]
                initialize_storage=False,
            )
        )

    def test_status_returns_active_batch(self) -> None:
        response = self.client.get("/api/publish/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pending_change_count"], 2)
        self.assertEqual(response.json()["active_job"]["job_id"], "publish-1")

    def test_post_promotes_current_batch_to_manual(self) -> None:
        response = self.client.post("/api/publish/jobs")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["trigger"], "manual")

    def test_missing_job_returns_404(self) -> None:
        response = self.client.get("/api/publish/jobs/missing")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
