from __future__ import annotations

import unittest
from datetime import datetime

from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.ingest import IngestJobResponse, IngestValidation
from app.services.ingest_service import (
    IngestConflictError,
    IngestNotFoundError,
    IngestValidationError,
)


class FakeIngestService:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.jobs = [
            IngestJobResponse(
                job_id="job-1",
                status="queued",
                stage="uploaded",
                progress_percent=0,
                original_filename="report.md",
                source_path="raw/uploads/20260624-153012-report.md",
                validation=IngestValidation(),
                created_at=datetime(2026, 6, 24, 15, 30, 12),
                updated_at=datetime(2026, 6, 24, 15, 30, 12),
            )
        ]

    async def create_job(self, *, file: UploadFile, auto_convert: bool = True) -> IngestJobResponse:
        if self.error is not None:
            raise self.error
        return self.jobs[0].model_copy(update={"original_filename": file.filename})

    def get_job(self, job_id: str) -> IngestJobResponse:
        if self.error is not None:
            raise self.error
        for job in self.jobs:
            if job.job_id == job_id:
                return job
        raise IngestNotFoundError(job_id)

    def list_jobs(self, limit: int) -> list[IngestJobResponse]:
        if self.error is not None:
            raise self.error
        return self.jobs[:limit]


class IngestApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeIngestService()
        self.client = TestClient(
            create_app(
                ingest_service=self.service,  # type: ignore[arg-type]
                initialize_storage=False,
            )
        )

    def test_create_ingest_job_accepts_upload(self) -> None:
        response = self.client.post(
            "/api/ingest/jobs",
            files={"file": ("report.md", b"# Report", "text/markdown")},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job_id"], "job-1")
        self.assertEqual(response.json()["status"], "queued")

    def test_create_ingest_job_maps_validation_error(self) -> None:
        self.service.error = IngestValidationError("unsupported file extension: .exe")

        response = self.client.post(
            "/api/ingest/jobs",
            files={"file": ("setup.exe", b"binary", "application/octet-stream")},
        )

        self.assertEqual(response.status_code, 422)

    def test_create_ingest_job_maps_conflict(self) -> None:
        self.service.error = IngestConflictError("upload already exists")

        response = self.client.post(
            "/api/ingest/jobs",
            files={"file": ("report.md", b"# Report", "text/markdown")},
        )

        self.assertEqual(response.status_code, 409)

    def test_get_ingest_job_returns_job(self) -> None:
        response = self.client.get("/api/ingest/jobs/job-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "job_id": "job-1",
                "status": "queued",
                "stage": "uploaded",
                "progress_percent": 0,
                "original_filename": "report.md",
                "source_path": "raw/uploads/20260624-153012-report.md",
                "created_pages": [],
                "updated_pages": [],
                "contradictions": [],
                "validation": {"broken_links": [], "unindexed": []},
                "error": None,
                "created_at": "2026-06-24T15:30:12",
                "started_at": None,
                "updated_at": "2026-06-24T15:30:12",
                "finished_at": None,
            },
        )

    def test_get_ingest_job_maps_missing_job(self) -> None:
        response = self.client.get("/api/ingest/jobs/missing")

        self.assertEqual(response.status_code, 404)

    def test_list_ingest_jobs_returns_recent_jobs(self) -> None:
        response = self.client.get("/api/ingest/jobs?limit=20")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["job_id"], "job-1")


if __name__ == "__main__":
    unittest.main()
