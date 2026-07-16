from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import UploadFile

from app.schemas.ingest import IngestJobResponse, IngestValidation
from app.services.ingest_service import IngestConflictError, IngestService, IngestValidationError


class FakeStorage:
    def __init__(self) -> None:
        self.jobs: dict[str, IngestJobResponse] = {}
        self.running: list[str] = []
        self.succeeded: list[str] = []
        self.failed: list[str] = []

    def create_ingest_job(
        self,
        *,
        job_id: str,
        status: str,
        original_filename: str,
        stored_filename: str,
        source_path: str,
        created_at: datetime,
    ) -> IngestJobResponse:
        job = IngestJobResponse(
            job_id=job_id,
            status=status,
            original_filename=original_filename,
            source_path=source_path,
            validation=IngestValidation(),
            created_at=created_at,
        )
        self.jobs[job_id] = job
        return job

    def get_ingest_job(self, job_id: str) -> IngestJobResponse | None:
        return self.jobs.get(job_id)

    def list_ingest_jobs(self, limit: int) -> list[IngestJobResponse]:
        return list(self.jobs.values())[:limit]

    def mark_ingest_job_running(self, job_id: str, started_at: datetime) -> None:
        self.running.append(job_id)

    def mark_ingest_job_succeeded(
        self,
        *,
        job_id: str,
        created_pages: list[str],
        updated_pages: list[str],
        contradictions: list[str],
        validation: IngestValidation,
        finished_at: datetime,
    ) -> None:
        self.succeeded.append(job_id)

    def mark_ingest_job_failed(self, *, job_id: str, error: str, finished_at: datetime) -> None:
        self.failed.append(job_id)


class IngestServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.agent_root = Path(self.temp_dir.name)
        (self.agent_root / "wiki").mkdir()
        self.storage = FakeStorage()
        with patch.object(IngestService, "_load_llm_caller", return_value=lambda prompt, max_tokens=None: "{}"):
            self.service = IngestService(
                storage=self.storage,
                agent_root=self.agent_root,
                start_worker=False,
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_job_saves_markdown_upload(self) -> None:
        upload = UploadFile(filename="../report.md", file=io.BytesIO(b"# Report"))

        job = asyncio.run(self.service.create_job(file=upload))

        self.assertEqual(job.status, "queued")
        self.assertEqual(job.original_filename, "report.md")
        self.assertTrue((self.agent_root / job.source_path).exists())
        self.assertRegex(job.source_path, r"^raw/uploads/\d{8}-\d{6}-report\.md$")

    def test_create_job_rejects_empty_file(self) -> None:
        upload = UploadFile(filename="empty.md", file=io.BytesIO(b""))

        with self.assertRaises(IngestValidationError):
            asyncio.run(self.service.create_job(file=upload))

    def test_create_job_rejects_unsupported_extension(self) -> None:
        upload = UploadFile(filename="setup.exe", file=io.BytesIO(b"binary"))

        with self.assertRaises(IngestValidationError):
            asyncio.run(self.service.create_job(file=upload))

    def test_create_job_detects_same_second_filename_conflict(self) -> None:
        existing = self.agent_root / "raw" / "uploads" / "20260624-153012-report.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("old", encoding="utf-8")
        upload = UploadFile(filename="report.md", file=io.BytesIO(b"# Report"))

        with patch("app.services.ingest_service.datetime") as fake_datetime:
            fake_datetime.utcnow.return_value = datetime(2026, 6, 24, 15, 30, 12)
            with self.assertRaises(IngestConflictError):
                asyncio.run(self.service.create_job(file=upload))

    def test_parse_llm_result_retries_when_first_response_has_no_json(self) -> None:
        source_path = self.agent_root / "raw" / "uploads" / "report.pdf"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("pdf", encoding="utf-8")
        retry_payload = (
            '{"title":"Report","slug":"report","source_page":"# Report",'
            '"index_entry":"- [Report](sources/report.md) - summary",'
            '"overview_update":null,"entity_pages":[],"concept_pages":[],'
            '"contradictions":[],"log_entry":"## log"}'
        )
        self.service._call_llm_main = lambda prompt, max_tokens=None: retry_payload  # type: ignore[method-assign]

        parsed = self.service._parse_llm_result_with_repair(
            prompt="prompt",
            raw="I cannot produce JSON for this document.",
            source_path=source_path,
            job_id="job-1",
        )

        self.assertEqual(parsed["slug"], "report")
        self.assertTrue((source_path.parent / "report.job-1.initial.llm-response.txt").exists())

    def test_build_prompt_forces_null_overview_update(self) -> None:
        source = self.agent_root / "raw" / "uploads" / "report.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Report", encoding="utf-8")

        prompt = self.service._build_prompt(source=source, source_content="# Report")

        self.assertIn("LLM Wiki Agent — Schema & Workflow Instructions", prompt)
        self.assertIn('"overview_update": null', prompt)
        self.assertIn('Always set `"overview_update"` to `null`', prompt)

    def test_build_wiki_context_clips_large_overview(self) -> None:
        overview_path = self.agent_root / "wiki" / "overview.md"
        overview_path.write_text("line\n" * 4000, encoding="utf-8")

        context = self.service._build_wiki_context()

        self.assertIn("[context clipped to", context)
        self.assertLess(len(context), 12000)


if __name__ == "__main__":
    unittest.main()
