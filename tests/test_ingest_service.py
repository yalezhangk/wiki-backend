from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import UploadFile

from app.llm_config import LLMConfigError, LLMResponseTruncatedError
from app.schemas.ingest import IngestJobResponse, IngestValidation
from app.services.ingest_service import (
    IngestConflictError,
    IngestContentQualityError,
    IngestLLMInvalidJSONError,
    IngestLLMResponseTruncatedError,
    IngestService,
    IngestValidationError,
)
from app.storage.mysql import MySQLStorage


class FakeMigrationCursor:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, query: str) -> None:
        self.queries.append(" ".join(query.split()))

    def fetchall(self) -> list[dict[str, str]]:
        return []

    def fetchone(self) -> None:
        return None


class FakeScheduledRecoveryCursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.queries: list[tuple[str, tuple[object, ...] | None]] = []
        self.rows = rows

    def execute(self, query: str, values: tuple[object, ...] | None = None) -> None:
        self.queries.append((" ".join(query.split()), values))

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class FakeScheduledRecoveryConnection:
    def __init__(self, cursor: FakeScheduledRecoveryCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "FakeScheduledRecoveryConnection":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def cursor(self) -> "FakeScheduledRecoveryConnection":
        return self

    def execute(self, query: str, values: tuple[object, ...] | None = None) -> None:
        self._cursor.execute(query, values)

    def fetchall(self) -> list[dict[str, object]]:
        return self._cursor.fetchall()


class FakeStorage:
    def __init__(self) -> None:
        self.jobs: dict[int, IngestJobResponse] = {}
        self.running: list[int] = []
        self.succeeded: list[int] = []
        self.failed: list[int] = []
        self.progress_updates: list[tuple[str, int]] = []

    def create_ingest_job(
        self,
        *,
        status: str,
        original_filename: str,
        stored_filename: str,
        source_path: str,
        trigger: str = "manual",
        created_at: datetime,
    ) -> IngestJobResponse:
        job_id = len(self.jobs) + 1
        job = IngestJobResponse(
            job_id=job_id,
            status=status,
            stage="uploaded",
            progress_percent=0,
            original_filename=original_filename,
            trigger=trigger,
            source_path=source_path,
            validation=IngestValidation(),
            created_at=created_at,
            updated_at=created_at,
        )
        self.jobs[job_id] = job
        return job

    def get_ingest_job(self, job_id: int) -> IngestJobResponse | None:
        return self.jobs.get(job_id)

    def list_ingest_jobs(self, limit: int) -> list[IngestJobResponse]:
        return list(self.jobs.values())[:limit]

    def mark_ingest_job_running(self, job_id: int, started_at: datetime) -> None:
        self.running.append(job_id)
        self.jobs[job_id] = self.jobs[job_id].model_copy(
            update={"status": "running", "started_at": started_at, "updated_at": started_at}
        )

    def update_ingest_job_progress(
        self,
        *,
        job_id: int,
        stage: str,
        progress_percent: int,
        updated_at: datetime,
    ) -> None:
        self.progress_updates.append((stage, progress_percent))
        self.jobs[job_id] = self.jobs[job_id].model_copy(
            update={
                "stage": stage,
                "progress_percent": progress_percent,
                "updated_at": updated_at,
            }
        )

    def mark_ingest_job_succeeded(
        self,
        *,
        job_id: int,
        created_pages: list[str],
        updated_pages: list[str],
        contradictions: list[str],
        validation: IngestValidation,
        finished_at: datetime,
    ) -> None:
        self.succeeded.append(job_id)
        self.jobs[job_id] = self.jobs[job_id].model_copy(
            update={
                "status": "succeeded",
                "stage": "completed",
                "progress_percent": 100,
                "created_pages": created_pages,
                "updated_pages": updated_pages,
                "contradictions": contradictions,
                "validation": validation,
                "updated_at": finished_at,
                "finished_at": finished_at,
            }
        )

    def mark_ingest_job_failed(self, *, job_id: int, error: str, finished_at: datetime) -> None:
        self.failed.append(job_id)
        self.jobs[job_id] = self.jobs[job_id].model_copy(
            update={
                "status": "failed",
                "error": error,
                "updated_at": finished_at,
                "finished_at": finished_at,
            }
        )


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
        self.assertEqual(job.stage, "uploaded")
        self.assertEqual(job.progress_percent, 0)
        self.assertEqual(job.updated_at, job.created_at)
        self.assertEqual(job.original_filename, "report.md")
        self.assertEqual(job.trigger, "manual")
        self.assertTrue((self.agent_root / job.source_path).exists())
        self.assertEqual(job.source_path, "raw/uploads/report.md")

    def test_scheduled_job_uses_unique_stored_filename_and_trigger(self) -> None:
        first_upload = UploadFile(filename="report.md", file=io.BytesIO(b"# Report"))
        second_upload = UploadFile(filename="report.md", file=io.BytesIO(b"# Report"))

        first_job = asyncio.run(self.service.create_job(file=first_upload, trigger="scheduled"))
        second_job = asyncio.run(self.service.create_job(file=second_upload, trigger="scheduled"))

        self.assertEqual(first_job.trigger, "scheduled")
        self.assertEqual(second_job.trigger, "scheduled")
        self.assertNotEqual(first_job.source_path, second_job.source_path)

    def test_create_job_rejects_empty_file(self) -> None:
        upload = UploadFile(filename="empty.md", file=io.BytesIO(b""))

        with self.assertRaises(IngestValidationError):
            asyncio.run(self.service.create_job(file=upload))

    def test_create_job_rejects_unsupported_extension(self) -> None:
        upload = UploadFile(filename="setup.exe", file=io.BytesIO(b"binary"))

        with self.assertRaises(IngestValidationError):
            asyncio.run(self.service.create_job(file=upload))

    def test_create_job_rejects_oversize_upload_and_removes_partial_file(self) -> None:
        service = IngestService(
            storage=self.storage,
            agent_root=self.agent_root,
            start_worker=False,
            max_upload_bytes=4,
        )
        upload = UploadFile(filename="report.md", file=io.BytesIO(b"12345"))

        with self.assertRaisesRegex(IngestValidationError, "maximum upload size"):
            asyncio.run(service.create_job(file=upload))

        self.assertEqual(list((self.agent_root / "raw" / "uploads").iterdir()), [])

    def test_create_job_rejects_declared_content_type_mismatch(self) -> None:
        upload = UploadFile(
            filename="report.pdf",
            file=io.BytesIO(b"%PDF-1.7"),
            headers={"content-type": "text/plain"},
        )

        with self.assertRaisesRegex(IngestValidationError, "content type"):
            asyncio.run(self.service.create_job(file=upload))

    def test_create_job_rejects_invalid_pdf_signature(self) -> None:
        upload = UploadFile(
            filename="report.pdf",
            file=io.BytesIO(b"not a pdf"),
            headers={"content-type": "application/pdf"},
        )

        with self.assertRaisesRegex(IngestValidationError, "does not match .pdf"):
            asyncio.run(self.service.create_job(file=upload))

    def test_create_job_detects_filename_conflict(self) -> None:
        existing = self.agent_root / "raw" / "uploads" / "report.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("old", encoding="utf-8")
        upload = UploadFile(filename="report.md", file=io.BytesIO(b"# Report"))

        with self.assertRaisesRegex(IngestConflictError, "上传文件已存在，请修改文件名后重试"):
            asyncio.run(self.service.create_job(file=upload))

    def test_parse_llm_result_retries_when_first_response_has_no_json(self) -> None:
        source_path = self.agent_root / "raw" / "uploads" / "report.pdf"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("pdf", encoding="utf-8")
        retry_payload = (
            '{"ingest_status":"succeeded","ingest_error":null,"title":"Report","slug":"report","source_page":"# Report",'
            '"index_entry":"- [Report](sources/report.md) - summary",'
            '"overview_update":null,"entity_pages":[],"concept_pages":[],'
            '"contradictions":[],"log_entry":"## log"}'
        )
        self.service._call_llm_main = lambda prompt, max_tokens=None: retry_payload  # type: ignore[method-assign]

        parsed = self.service._parse_llm_result_with_repair(
            prompt="prompt",
            raw="I cannot produce JSON for this document.",
            source_path=source_path,
            job_id=1,
        )

        self.assertEqual(parsed["slug"], "report")
        self.assertTrue((source_path.parent / "report.1.initial.llm-response.txt").exists())

    def test_parse_truncated_json_is_not_sent_to_json_repair(self) -> None:
        source_path = self.agent_root / "raw" / "uploads" / "report.md"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("# Report", encoding="utf-8")
        call_count = 0

        def unexpected_repair(prompt: str, max_tokens: int | None = None) -> str:
            nonlocal call_count
            call_count += 1
            return "{}"

        self.service._call_llm_main = unexpected_repair  # type: ignore[method-assign]

        with self.assertRaises(IngestLLMResponseTruncatedError):
            self.service._parse_llm_result_with_repair(
                prompt="prompt",
                raw='{"title":"unfinished',
                source_path=source_path,
                job_id=2,
            )

        self.assertEqual(call_count, 0)
        self.assertTrue(
            (source_path.parent / "report.2.initial.llm-response.txt").exists()
        )

    def test_provider_truncation_saves_partial_response_before_job_fails(self) -> None:
        upload = UploadFile(filename="report.md", file=io.BytesIO(b"# Report"))
        job = asyncio.run(self.service.create_job(file=upload))
        partial_response = '{"title":"Report","slug":"report"'

        def truncated_caller(prompt: str, max_tokens: int | None = None) -> str:
            raise LLMResponseTruncatedError(
                model="deepseek/deepseek-v4-pro",
                max_tokens=max_tokens or 0,
                finish_reason="length",
                response_content=partial_response,
            )

        self.service._call_llm_main = truncated_caller  # type: ignore[method-assign]

        self.service._run_job(job.job_id)

        failed = self.storage.jobs[job.job_id]
        upload_path = self.agent_root / job.source_path
        debug_path = upload_path.parent / f"{upload_path.stem}.{job.job_id}.truncated.llm-response.txt"
        self.assertTrue(failed.error.startswith("llm_response_truncated:"))
        self.assertEqual(debug_path.read_text(encoding="utf-8"), partial_response)
        self.assertFalse((self.agent_root / "wiki" / "sources" / "report.md").exists())

    def test_invalid_json_failure_does_not_expose_debug_paths(self) -> None:
        source_path = self.agent_root / "raw" / "uploads" / "report.md"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("# Report", encoding="utf-8")
        self.service._call_llm_main = lambda prompt, max_tokens=None: "still not json"  # type: ignore[method-assign]

        with self.assertRaises(IngestLLMInvalidJSONError) as context:
            self.service._parse_llm_result_with_repair(
                prompt="prompt",
                raw="not json",
                source_path=source_path,
                job_id=3,
            )

        self.assertTrue(str(context.exception).startswith("llm_json_invalid:"))
        self.assertNotIn(".llm-response.txt", str(context.exception))

    def test_llm_token_budget_is_passed_to_ingest_call(self) -> None:
        service = IngestService(
            storage=self.storage,
            agent_root=self.agent_root,
            start_worker=False,
            ingest_llm_max_tokens=12288,
        )
        observed_max_tokens: list[int | None] = []
        service._call_llm_main = lambda prompt, max_tokens=None: (  # type: ignore[method-assign]
            observed_max_tokens.append(max_tokens) or "{}"
        )

        self.assertEqual(service._call_llm_with_retry("prompt"), "{}")
        self.assertEqual(observed_max_tokens, [12288])

    def test_empty_response_retries_once(self) -> None:
        calls = 0

        def caller(prompt: str, max_tokens: int | None = None) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise LLMConfigError("LLM returned an empty response")
            return "{}"

        self.service._call_llm_main = caller  # type: ignore[method-assign]
        with patch("app.services.ingest_service.time.sleep"):
            self.assertEqual(self.service._call_llm_with_retry("prompt"), "{}")

        self.assertEqual(calls, 2)

    def test_authentication_error_is_not_retried(self) -> None:
        calls = 0

        def caller(prompt: str, max_tokens: int | None = None) -> str:
            nonlocal calls
            calls += 1
            raise PermissionError("invalid API key")

        self.service._call_llm_main = caller  # type: ignore[method-assign]

        with self.assertRaises(PermissionError):
            self.service._call_llm_with_retry("prompt")

        self.assertEqual(calls, 1)

    def test_schema_mismatch_saves_diagnostic_and_reports_stable_error(self) -> None:
        upload = UploadFile(filename="report.md", file=io.BytesIO(b"# Report"))
        job = asyncio.run(self.service.create_job(file=upload))
        self.service._call_llm_main = lambda prompt, max_tokens=None: '{"title":"Report"}'  # type: ignore[method-assign]

        self.service._run_job(job.job_id)

        failed = self.storage.jobs[job.job_id]
        self.assertTrue(failed.error.startswith("llm_schema_invalid:"))
        upload_path = self.agent_root / job.source_path
        self.assertTrue(
            (upload_path.parent / f"{upload_path.stem}.{job.job_id}.schema.llm-response.txt").exists()
        )

    def test_llm_reported_failure_does_not_write_wiki_or_mark_job_succeeded(self) -> None:
        upload = UploadFile(filename="report.md", file=io.BytesIO(b"# Report"))
        job = asyncio.run(self.service.create_job(file=upload))
        self.service._call_llm_main = lambda prompt, max_tokens=None: (  # type: ignore[method-assign]
            '{"ingest_status":"failed","ingest_error":"source contains no usable text"}'
        )

        self.service._run_job(job.job_id)

        failed = self.storage.jobs[job.job_id]
        self.assertEqual(failed.status, "failed")
        self.assertTrue(failed.error.startswith("llm_ingest_failed:"))
        self.assertNotIn(job.job_id, self.storage.succeeded)
        self.assertFalse((self.agent_root / "wiki" / "sources" / "report.md").exists())
        self.assertNotIn(("writing_wiki", 65), self.storage.progress_updates)

    def test_low_quality_pdf_conversion_does_not_call_llm_or_write_wiki(self) -> None:
        upload = UploadFile(filename="report.pdf", file=io.BytesIO(b"%PDF-1.7"))
        job = asyncio.run(self.service.create_job(file=upload))
        converted = self.agent_root / "raw" / "uploads" / "converted.md"
        converted.write_text("◇" * 100, encoding="utf-8")

        def unexpected_llm_call(prompt: str, max_tokens: int | None = None) -> str:
            raise AssertionError("低质量转换结果不应发送给 LLM")

        self.service._call_llm_main = unexpected_llm_call  # type: ignore[method-assign]
        with patch.object(
            self.service,
            "_extract_pdf_page_texts",
            return_value=["This PDF has selectable text before conversion."],
        ), patch.object(
            self.service,
            "_convert_to_markdown",
            return_value=converted,
        ), patch.object(
            self.service,
            "_convert_pdf_with_marker",
            return_value=converted,
        ) as marker_converter:
            self.service._run_job(job.job_id)

        marker_converter.assert_called_once_with(self.agent_root / job.source_path)
        failed = self.storage.jobs[job.job_id]
        self.assertEqual(failed.status, "failed")
        self.assertTrue(failed.error.startswith("ocr_failed:"))
        self.assertEqual(self.storage.progress_updates, [("converting", 10)])
        self.assertFalse((self.agent_root / "wiki" / "sources" / "report.md").exists())

    def test_scanned_pdf_attempts_ocr_before_calling_llm(self) -> None:
        upload = UploadFile(filename="scan.pdf", file=io.BytesIO(b"%PDF-1.7"))
        job = asyncio.run(self.service.create_job(file=upload))
        converted = self.agent_root / "raw" / "uploads" / "scan.md"
        converted.write_text("# Scan\n\nOCR extracted useful whitepaper text.", encoding="utf-8")
        llm_payload = (
            '{"ingest_status":"succeeded","ingest_error":null,"title":"Scan","slug":"scan",'
            '"source_page":"# Scan","index_entry":"- [Scan](sources/scan.md) - summary",'
            '"overview_update":null,"entity_pages":[],"concept_pages":[],'
            '"contradictions":[],"log_entry":"## log"}'
        )
        self.service._call_llm_main = lambda prompt, max_tokens=None: llm_payload  # type: ignore[method-assign]

        with patch.object(self.service, "_extract_pdf_page_texts", return_value=["", " "]), patch.object(
            self.service,
            "_convert_pdf_with_marker",
            return_value=converted,
        ) as marker_converter:
            self.service._run_job(job.job_id)

        marker_converter.assert_called_once_with(self.agent_root / job.source_path)
        completed = self.storage.jobs[job.job_id]
        self.assertEqual(completed.status, "succeeded")
        self.assertTrue((self.agent_root / "wiki" / "sources" / "scan.md").exists())

    def test_scanned_pdf_fails_without_calling_llm_when_ocr_is_unavailable(self) -> None:
        upload = UploadFile(filename="scan.pdf", file=io.BytesIO(b"%PDF-1.7"))
        job = asyncio.run(self.service.create_job(file=upload))

        def unexpected_llm_call(prompt: str, max_tokens: int | None = None) -> str:
            raise AssertionError("OCR 不可用时不应调用 LLM")

        self.service._call_llm_main = unexpected_llm_call  # type: ignore[method-assign]
        with patch.object(self.service, "_extract_pdf_page_texts", return_value=["", " "]), patch.object(
            self.service,
            "_convert_pdf_with_marker",
            side_effect=IngestContentQualityError(
                "ocr_unavailable",
                "扫描 PDF 需要 RapidOCR，但服务未安装 rapidocr 或 onnxruntime。",
            ),
        ):
            self.service._run_job(job.job_id)

        failed = self.storage.jobs[job.job_id]
        self.assertEqual(failed.status, "failed")
        self.assertTrue(failed.error.startswith("ocr_unavailable:"))
        self.assertEqual(self.storage.progress_updates, [("converting", 10)])
        self.assertFalse((self.agent_root / "wiki" / "sources" / "scan.md").exists())

    def test_default_pdf_ocr_uses_rapidocr_without_starting_marker(self) -> None:
        source = self.agent_root / "raw" / "uploads" / "scan.pdf"
        converted = self.agent_root / "raw" / "uploads" / "scan.md"

        with patch("app.services.ingest_service.settings.ingest_enable_marker_ocr", False), patch(
            "app.services.ingest_service.shutil.which"
        ) as marker_command, patch.object(
            self.service,
            "_convert_pdf_with_rapidocr",
            return_value=converted,
        ) as rapidocr_converter:
            result = self.service._convert_pdf_with_marker(source)

        self.assertEqual(result, converted)
        rapidocr_converter.assert_called_once_with(source)
        marker_command.assert_not_called()

    def test_enabled_marker_without_markdown_falls_back_to_rapidocr(self) -> None:
        source = self.agent_root / "raw" / "uploads" / "scan.pdf"
        converted = self.agent_root / "raw" / "uploads" / "scan.md"

        with patch("app.services.ingest_service.settings.ingest_enable_marker_ocr", True), patch(
            "app.services.ingest_service.shutil.which",
            return_value="marker_single",
        ), patch(
            "app.services.ingest_service.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stderr=""),
        ), patch.object(
            self.service,
            "_convert_pdf_with_rapidocr",
            return_value=converted,
        ) as rapidocr_converter:
            result = self.service._convert_pdf_with_marker(source)

        self.assertEqual(result, converted)
        rapidocr_converter.assert_called_once_with(source)

    def test_build_prompt_allows_conditional_overview_update(self) -> None:
        source = self.agent_root / "raw" / "uploads" / "report.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Report", encoding="utf-8")

        prompt = self.service._build_prompt(source=source, source_content="# Report")

        self.assertIn("LLM Wiki Agent — Schema & Workflow Instructions", prompt)
        self.assertIn("full updated content for wiki/overview.md", prompt)
        self.assertIn("otherwise return `null`", prompt)
        self.assertIn('"ingest_status": "succeeded"', prompt)
        self.assertIn('"ingest_status": "failed"', prompt)

    def test_build_wiki_context_keeps_complete_index_overview_and_recent_source(self) -> None:
        index_path = self.agent_root / "wiki" / "index.md"
        overview_path = self.agent_root / "wiki" / "overview.md"
        recent_source_path = self.agent_root / "wiki" / "sources" / "recent.md"
        index_content = "index-start\n" + ("index-body\n" * 2000) + "index-end"
        overview_content = "overview-start\n" + ("overview-body\n" * 2000) + "overview-end"
        recent_source_content = "source-start\n" + ("source-body\n" * 2000) + "source-end"
        index_path.write_text(index_content, encoding="utf-8")
        overview_path.write_text(overview_content, encoding="utf-8")
        recent_source_path.parent.mkdir(parents=True, exist_ok=True)
        recent_source_path.write_text(recent_source_content, encoding="utf-8")

        context = self.service._build_wiki_context()

        self.assertIn(index_content, context)
        self.assertIn(overview_content, context)
        self.assertIn(recent_source_content, context)
        self.assertNotIn("[context clipped to", context)

    def test_markdown_job_persists_real_stages_in_order(self) -> None:
        upload = UploadFile(filename="report.md", file=io.BytesIO(b"# Report"))
        job = asyncio.run(self.service.create_job(file=upload))
        llm_payload = (
            '{"ingest_status":"succeeded","ingest_error":null,"title":"Report","slug":"report","source_page":"# Report",'
            '"index_entry":"- [Report](sources/report.md) - summary",'
            '"overview_update":null,"entity_pages":[],"concept_pages":[],'
            '"contradictions":[],"log_entry":"## log"}'
        )
        self.service._call_llm_main = lambda prompt, max_tokens=None: llm_payload  # type: ignore[method-assign]

        self.service._run_job(job.job_id)

        self.assertEqual(
            self.storage.progress_updates,
            [
                ("extracting", 35),
                ("writing_wiki", 65),
                ("validating", 85),
            ],
        )
        completed = self.storage.jobs[job.job_id]
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.stage, "completed")
        self.assertEqual(completed.progress_percent, 100)

    def test_non_markdown_job_starts_with_converting_stage(self) -> None:
        upload = UploadFile(filename="report.pdf", file=io.BytesIO(b"%PDF-1.7"))
        job = asyncio.run(self.service.create_job(file=upload))
        converted = self.agent_root / "raw" / "uploads" / "converted.md"
        converted.write_text("# Report\n\nConverted PDF content is long enough for extraction.", encoding="utf-8")
        llm_payload = (
            '{"ingest_status":"succeeded","ingest_error":null,"title":"Report","slug":"report","source_page":"# Report",'
            '"index_entry":"- [Report](sources/report.md) - summary",'
            '"overview_update":null,"entity_pages":[],"concept_pages":[],'
            '"contradictions":[],"log_entry":"## log"}'
        )
        self.service._call_llm_main = lambda prompt, max_tokens=None: llm_payload  # type: ignore[method-assign]

        with patch.object(
            self.service,
            "_extract_pdf_page_texts",
            return_value=["This PDF has selectable text before conversion."],
        ), patch.object(self.service, "_convert_to_markdown", return_value=converted):
            self.service._run_job(job.job_id)

        self.assertEqual(self.storage.progress_updates[0], ("converting", 10))
        self.assertEqual(self.storage.jobs[job.job_id].status, "succeeded")

    def test_ingest_classifies_created_and_updated_pages_from_prewrite_state(self) -> None:
        existing_entity = self.agent_root / "wiki" / "entities" / "Existing.md"
        existing_entity.parent.mkdir(parents=True)
        existing_entity.write_text("# Old", encoding="utf-8")
        upload = UploadFile(filename="report.md", file=io.BytesIO(b"# Report"))
        job = asyncio.run(self.service.create_job(file=upload))
        llm_payload = (
            '{"ingest_status":"succeeded","ingest_error":null,"title":"Report","slug":"report","source_page":"# Report",'
            '"index_entry":"- [Report](sources/report.md) - summary",'
            '"overview_update":null,'
            '"entity_pages":[{"path":"entities/Existing.md","content":"# New"}],'
            '"concept_pages":[],"contradictions":[],"log_entry":"## log"}'
        )
        self.service._call_llm_main = lambda prompt, max_tokens=None: llm_payload  # type: ignore[method-assign]

        self.service._run_job(job.job_id)

        completed = self.storage.jobs[job.job_id]
        self.assertEqual(
            completed.created_pages,
            ["sources/report.md", "index.md", "log.md"],
        )
        self.assertEqual(completed.updated_pages, ["entities/Existing.md"])
        self.assertEqual(existing_entity.read_text(encoding="utf-8"), "# New")

    def test_ingest_updates_overview_when_llm_returns_full_content(self) -> None:
        overview_path = self.agent_root / "wiki" / "overview.md"
        overview_path.write_text("# Old Overview", encoding="utf-8")
        upload = UploadFile(filename="report.md", file=io.BytesIO(b"# Report"))
        job = asyncio.run(self.service.create_job(file=upload))
        llm_payload = (
            '{"ingest_status":"succeeded","ingest_error":null,"title":"Report","slug":"report","source_page":"# Report",'
            '"index_entry":"- [Report](sources/report.md) - summary",'
            '"overview_update":"# New Overview","entity_pages":[],"concept_pages":[],'
            '"contradictions":[],"log_entry":"## log"}'
        )
        self.service._call_llm_main = lambda prompt, max_tokens=None: llm_payload  # type: ignore[method-assign]

        self.service._run_job(job.job_id)

        completed = self.storage.jobs[job.job_id]
        self.assertIn("overview.md", completed.updated_pages)
        self.assertEqual(overview_path.read_text(encoding="utf-8"), "# New Overview")

    def test_append_log_inserts_new_entry_below_template_separator(self) -> None:
        log_path = self.agent_root / "wiki" / "log.md"
        template = (
            "# Wiki Log\n\n"
            "Newest-first chronological record of all operations.\n\n"
            "Format: `## [YYYY-MM-DD] <operation> | <title>`\n\n"
            r'Parse recent entries: `grep "^## \[" wiki/log.md | head -10`'
            "\n\n"
            "---\n\n"
        )
        old_entry = "## [2026-07-21] ingest | Old\n\nOld details.\n"
        log_path.write_text(template + old_entry, encoding="utf-8")

        self.service._append_log("## [2026-07-22] ingest | New\n\nNew details.")

        content = log_path.read_text(encoding="utf-8")
        self.assertTrue(content.startswith(template))
        self.assertLess(content.index("| New"), content.index("| Old"))

    def test_append_log_without_template_keeps_prepend_compatibility(self) -> None:
        log_path = self.agent_root / "wiki" / "log.md"
        log_path.write_text("## [2026-07-21] ingest | Old\n", encoding="utf-8")

        self.service._append_log("## [2026-07-22] ingest | New")

        content = log_path.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("## [2026-07-22] ingest | New\n\n"))

    def test_append_log_repairs_entry_written_before_template(self) -> None:
        log_path = self.agent_root / "wiki" / "log.md"
        misplaced_entry = "## [2026-07-21] ingest | Misplaced\n\nMisplaced details.\n\n"
        template = "# Wiki Log\n\nNewest-first chronological record.\n\n---\n\n"
        old_entry = "## [2026-07-20] ingest | Old\n\nOld details.\n"
        log_path.write_text(misplaced_entry + template + old_entry, encoding="utf-8")

        self.service._append_log("## [2026-07-22] ingest | New")

        content = log_path.read_text(encoding="utf-8")
        self.assertTrue(content.startswith(template))
        self.assertLess(content.index("| New"), content.index("| Misplaced"))
        self.assertLess(content.index("| Misplaced"), content.index("| Old"))

    def test_mysql_upgrade_adds_and_backfills_ingest_progress_columns(self) -> None:
        cursor = FakeMigrationCursor()

        MySQLStorage._ensure_ingest_progress_columns(cursor)

        statements = "\n".join(cursor.queries)
        self.assertIn("ADD COLUMN stage", statements)
        self.assertIn("ADD COLUMN progress_percent", statements)
        self.assertIn("ADD COLUMN updated_at", statements)
        self.assertIn("WHEN status = 'succeeded' THEN 'completed'", statements)
        self.assertIn("WHEN status = 'running' THEN 'extracting'", statements)
        self.assertIn("COALESCE(finished_at, started_at, created_at)", statements)
        self.assertIn("MODIFY COLUMN updated_at DATETIME NOT NULL", statements)

    def test_mysql_upgrade_adds_ingest_trigger_column(self) -> None:
        cursor = FakeMigrationCursor()

        MySQLStorage._ensure_ingest_trigger_column(cursor)

        self.assertIn("ADD COLUMN `trigger` VARCHAR(32) NOT NULL DEFAULT 'manual'", "\n".join(cursor.queries))

    def test_mysql_recovery_marks_unknown_or_nonterminal_scheduled_jobs_failed(self) -> None:
        now = datetime(2026, 8, 3, 3, 0, 0)
        cursor = FakeScheduledRecoveryCursor(
            [
                {
                    "id": 1,
                    "relative_path": "unknown.md",
                    "ingest_job_id": None,
                    "ingest_status": None,
                    "ingest_error": None,
                },
                {
                    "id": 2,
                    "relative_path": "queued.md",
                    "ingest_job_id": 9,
                    "ingest_status": "queued",
                    "ingest_error": None,
                },
            ]
        )
        storage = MySQLStorage("host", 3306, "user", "password", "database")

        with patch.object(storage, "connect", return_value=FakeScheduledRecoveryConnection(cursor)):
            errors = storage.recover_scheduled_ingest_sources(now=now)

        statements = "\n".join(query for query, _ in cursor.queries)
        self.assertEqual(len(errors), 2)
        self.assertNotIn("DELETE FROM scheduled_ingest_sources", statements)
        self.assertEqual(statements.count("SET state = 'failed'"), 2)

    def test_failure_preserves_last_persisted_stage(self) -> None:
        upload = UploadFile(filename="report.md", file=io.BytesIO(b"# Report"))
        job = asyncio.run(self.service.create_job(file=upload))
        llm_payload = (
            '{"ingest_status":"succeeded","ingest_error":null,"title":"Report","slug":"report","source_page":"# Report",'
            '"index_entry":"- [Report](sources/report.md) - summary",'
            '"overview_update":null,"entity_pages":[],"concept_pages":[],'
            '"contradictions":[],"log_entry":"## log"}'
        )
        self.service._call_llm_main = lambda prompt, max_tokens=None: llm_payload  # type: ignore[method-assign]
        self.service._write_ingest_result = lambda data: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("write failed")
        )

        self.service._run_job(job.job_id)

        failed = self.storage.jobs[job.job_id]
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.stage, "writing_wiki")
        self.assertEqual(failed.progress_percent, 65)
        self.assertEqual(failed.error, "write failed")


if __name__ == "__main__":
    unittest.main()
