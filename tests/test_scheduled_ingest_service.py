from __future__ import annotations

import tempfile
import unittest
from collections import deque
from datetime import datetime
from pathlib import Path

from app.schemas.ingest import IngestJobResponse, IngestValidation
from app.services.scheduled_ingest_service import (
    LoopbackIngestApiClient,
    ScheduledIngestDuplicateError,
    ScheduledIngestError,
    ScheduledIngestService,
)
from app.storage.mysql import ScheduledIngestSource


class FakeScheduledStorage:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], ScheduledIngestSource] = {}
        self.file_identities: set[tuple[int, int]] = set()
        self.attempts: dict[int, int] = {}
        self.completed: dict[int, tuple[str, str | None]] = {}
        self.recovery_calls = 0

    def recover_scheduled_ingest_sources(self, *, now: datetime) -> list[str]:
        self.recovery_calls += 1
        return []

    def claim_scheduled_ingest_source(
        self,
        *,
        source_root: str,
        relative_path: str,
        source_device: int,
        source_inode: int,
        now: datetime,
    ) -> ScheduledIngestSource | None:
        key = (source_root, relative_path)
        if key in self.records or (source_device, source_inode) in self.file_identities:
            return None
        source = ScheduledIngestSource(
            source_id=len(self.records) + 1,
            source_root=source_root,
            relative_path=relative_path,
            state="processing",
            attempt_count=0,
            ingest_job_id=None,
        )
        self.records[key] = source
        self.file_identities.add((source_device, source_inode))
        return source

    def record_scheduled_ingest_attempt(
        self,
        *,
        source_id: int,
        ingest_job_id: int | None,
        attempted_at: datetime,
    ) -> None:
        self.attempts[source_id] = self.attempts.get(source_id, 0) + 1

    def set_scheduled_ingest_job(self, *, source_id: int, ingest_job_id: int) -> None:
        for key, record in self.records.items():
            if record.source_id == source_id:
                self.records[key] = ScheduledIngestSource(
                    source_id=record.source_id,
                    source_root=record.source_root,
                    relative_path=record.relative_path,
                    state=record.state,
                    attempt_count=self.attempts[source_id],
                    ingest_job_id=ingest_job_id,
                )
                return
        raise AssertionError(f"unknown source_id={source_id}")

    def complete_scheduled_ingest_source(
        self,
        *,
        source_id: int,
        state: str,
        error: str | None,
        finished_at: datetime,
    ) -> None:
        self.completed[source_id] = (state, error)


class FakeScheduledApiClient:
    def __init__(self, outcomes: dict[str, list[str]]) -> None:
        self._outcomes = {name: deque(values) for name, values in outcomes.items()}
        self._jobs: dict[int, IngestJobResponse] = {}
        self.uploads: list[str] = []

    def create_scheduled_job(
        self, *, source_path: Path, original_filename: str, source_url: str
    ) -> IngestJobResponse:
        self.uploads.append(original_filename)
        outcome = self._outcomes[original_filename].popleft()
        if outcome == "request_error":
            raise ScheduledIngestError("loopback ingest API is unavailable")
        if outcome == "duplicate":
            raise ScheduledIngestDuplicateError("文档名称已存在，跳过重复定时入库")
        job_id = len(self._jobs) + 1
        job = self._job(job_id=job_id, status=outcome)
        self._jobs[job_id] = job
        return job

    def get_job(self, job_id: int) -> IngestJobResponse:
        return self._jobs[job_id]

    @staticmethod
    def _job(*, job_id: int, status: str) -> IngestJobResponse:
        now = datetime(2026, 8, 3, 3, 0, 0)
        return IngestJobResponse(
            job_id=job_id,
            status=status,
            stage="completed" if status == "succeeded" else "extracting",
            progress_percent=100 if status == "succeeded" else 35,
            original_filename="source.md",
            trigger="scheduled",
            source_path="raw/uploads/source.md",
            validation=IngestValidation(),
            error="simulated ingest failure" if status == "failed" else None,
            created_at=now,
            updated_at=now,
            finished_at=now,
        )


class ScheduledIngestServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "A"
        self.root.mkdir()
        self.storage = FakeScheduledStorage()
        self.now = lambda: datetime(2026, 8, 3, 3, 0, 0)
        (self.root / "readme.txt").write_text("Source URL: https://example.com/root", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _service(self, api_client: FakeScheduledApiClient) -> ScheduledIngestService:
        return ScheduledIngestService(
            storage=self.storage,
            api_client=api_client,
            source_root=self.root,
            poll_seconds=0.01,
            poll_timeout_seconds=1,
            now=self.now,
            sleep=lambda _: None,
        )

    def test_first_scan_recurses_and_then_skips_already_recorded_paths(self) -> None:
        (self.root / "one.md").write_text("# one", encoding="utf-8")
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "readme.txt").write_text("Source URL: https://example.com/nested", encoding="utf-8")
        (nested / "one.md").write_text("# nested", encoding="utf-8")
        (nested / "ignored.txt").write_text("ignored", encoding="utf-8")
        api_client = FakeScheduledApiClient({"one.md": ["succeeded", "succeeded"]})
        service = self._service(api_client)

        first = service.run()
        second = service.run()

        self.assertEqual((first.scanned_count, first.candidate_count, first.succeeded_count), (2, 2, 2))
        self.assertEqual(second.skipped_count, 2)
        self.assertEqual(api_client.uploads, ["one.md", "one.md"])
        self.assertEqual(set(path for _, path in self.storage.records), {"one.md", "nested/one.md"})

    def test_failed_source_is_submitted_once_then_is_never_automatically_retried(self) -> None:
        (self.root / "failed.md").write_text("# failed", encoding="utf-8")
        api_client = FakeScheduledApiClient({"failed.md": ["failed"]})
        service = self._service(api_client)

        first = service.run()
        second = service.run()

        self.assertEqual(first.failed_count, 1)
        self.assertEqual(self.storage.attempts[1], 1)
        self.assertEqual(self.storage.completed[1][0], "failed")
        self.assertEqual(second.skipped_count, 1)
        self.assertEqual(api_client.uploads, ["failed.md"])

    def test_logs_scan_summary_and_already_recorded_source(self) -> None:
        (self.root / "known.md").write_text("# known", encoding="utf-8")
        api_client = FakeScheduledApiClient({"known.md": ["succeeded"]})
        service = self._service(api_client)

        with self.assertLogs("app.services.scheduled_ingest_service", level="INFO") as logs:
            service.run()
            service.run()

        output = "\n".join(logs.output)
        self.assertIn("Scheduled Markdown ingest scan started scanned=1", output)
        self.assertIn("Scheduled ingest succeeded relative_path=known.md job_id=1", output)
        self.assertIn(
            "Scheduled ingest skipped relative_path=known.md reason=already_recorded",
            output,
        )
        self.assertIn(
            "Scheduled Markdown ingest finished scanned=1 candidates=0 succeeded=0 failed=0 deferred=0 skipped=1",
            output,
        )

    def test_source_that_changes_while_snapshotting_is_deferred_without_a_record(self) -> None:
        source = self.root / "changing.md"
        source.write_text("# changing", encoding="utf-8")
        api_client = FakeScheduledApiClient({"changing.md": ["succeeded"]})
        service = self._service(api_client)
        service._create_stable_snapshot = lambda **_: None  # type: ignore[method-assign]

        summary = service.run()

        self.assertEqual(summary.deferred_count, 1)
        self.assertEqual(self.storage.records, {})
        self.assertEqual(api_client.uploads, [])

    def test_empty_markdown_is_recorded_as_a_final_failure_after_one_attempt(self) -> None:
        (self.root / "empty.md").write_bytes(b"")
        api_client = FakeScheduledApiClient({"empty.md": ["failed"]})

        summary = self._service(api_client).run()

        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(self.storage.attempts[1], 1)
        self.assertEqual(self.storage.completed[1][0], "failed")

    def test_renamed_source_is_not_treated_as_new(self) -> None:
        original = self.root / "original.md"
        original.write_text("# original", encoding="utf-8")
        api_client = FakeScheduledApiClient({"original.md": ["succeeded"]})
        service = self._service(api_client)

        service.run()
        original.rename(self.root / "renamed.md")
        second = service.run()

        self.assertEqual(second.candidate_count, 0)
        self.assertEqual(second.skipped_count, 1)
        self.assertEqual(api_client.uploads, ["original.md"])

    def test_symbolic_link_is_not_scanned(self) -> None:
        target = self.root / "target.md"
        target.write_text("# target", encoding="utf-8")
        link = self.root / "linked.md"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")
        api_client = FakeScheduledApiClient({"target.md": ["succeeded"]})

        summary = self._service(api_client).run()

        self.assertEqual(summary.scanned_count, 1)
        self.assertEqual(api_client.uploads, ["target.md"])

    def test_loopback_client_rejects_non_loopback_url(self) -> None:
        with self.assertRaises(ScheduledIngestError):
            LoopbackIngestApiClient(base_url="http://example.com:8081")

    def test_readme_requires_exactly_one_http_source_url_and_one_markdown(self) -> None:
        (self.root / "first.md").write_text("# first", encoding="utf-8")
        (self.root / "second.md").write_text("# second", encoding="utf-8")
        api_client = FakeScheduledApiClient({})

        summary = self._service(api_client).run()

        self.assertEqual(summary.failed_count, 2)
        self.assertEqual(api_client.uploads, [])

    def test_readme_missing_multiple_or_invalid_source_url_is_not_submitted(self) -> None:
        cases = {
            "missing": None,
            "multiple": "Source URL: https://example.com/one\nSource URL: https://example.com/two\n",
            "invalid": "Source URL: ftp://example.com/article\n",
        }
        for name, readme_content in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary_directory:
                source_root = Path(temporary_directory)
                (source_root / "article.md").write_text("# article", encoding="utf-8")
                if readme_content is not None:
                    (source_root / "readme.txt").write_text(readme_content, encoding="utf-8")
                api_client = FakeScheduledApiClient({})
                service = ScheduledIngestService(
                    storage=FakeScheduledStorage(),
                    api_client=api_client,
                    source_root=source_root,
                    poll_seconds=0.01,
                    poll_timeout_seconds=1,
                    now=self.now,
                    sleep=lambda _: None,
                )

                summary = service.run()

                self.assertEqual(summary.failed_count, 1)
                self.assertEqual(api_client.uploads, [])

    def test_bom_readme_source_url_is_sent_to_api(self) -> None:
        (self.root / "article.md").write_text("# article", encoding="utf-8")
        (self.root / "readme.txt").write_text(
            "Source URL: https://example.com/article\n",
            encoding="utf-8-sig",
        )
        api_client = FakeScheduledApiClient({"article.md": ["succeeded"]})

        summary = self._service(api_client).run()

        self.assertEqual(summary.succeeded_count, 1)
        self.assertEqual(api_client.uploads, ["article.md"])

    def test_duplicate_api_response_is_recorded_as_skip(self) -> None:
        (self.root / "duplicate.md").write_text("# duplicate", encoding="utf-8")
        api_client = FakeScheduledApiClient({"duplicate.md": ["duplicate"]})

        summary = self._service(api_client).run()

        self.assertEqual(summary.skipped_count, 1)
        self.assertEqual(self.storage.completed[1][0], "skipped")

    def test_loopback_payload_includes_source_url(self) -> None:
        source = self.root / "source.md"
        source.write_text("# source", encoding="utf-8")

        payload = LoopbackIngestApiClient._multipart_payload(
            boundary="boundary",
            source_path=source,
            original_filename="source.md",
            source_url="https://example.com/source",
        )

        self.assertIn(b'name="source_url"\r\n\r\nhttps://example.com/source', payload)


if __name__ == "__main__":
    unittest.main()
