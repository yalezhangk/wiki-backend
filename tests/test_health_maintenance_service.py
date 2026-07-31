from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from app.schemas.maintenance import MaintenanceJobResponse
from app.services.health_maintenance_service import HealthMaintenanceService
from tests.agent_parity_fixture import EXPECTED_HEALTH, create_agent_parity_wiki


class FakeStorage:
    def __init__(self) -> None:
        self.progress: list[tuple[str, int]] = []

    def update_maintenance_job_progress(
        self, *, job_id: int, stage: str, progress_percent: int, updated_at: datetime
    ) -> None:
        self.progress.append((stage, progress_percent))


class HealthMaintenanceServiceTests(unittest.TestCase):
    @staticmethod
    def _job(*, save_report: bool = True) -> MaintenanceJobResponse:
        now = datetime(2026, 7, 29, 10)
        return MaintenanceJobResponse(
            job_id=1,
            task_kind="health",
            status="running",
            result_state="unavailable",
            trigger="manual",
            stage="starting",
            progress_percent=5,
            options={"save_report": save_report},
            result_summary={},
            created_at=now,
            updated_at=now,
            started_at=now,
        )

    def test_reports_stub_index_differences_and_unlogged_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            source_dir = wiki / "sources"
            source_dir.mkdir(parents=True)
            (wiki / "index.md").write_text("[Missing](sources/missing.md)\n", encoding="utf-8")
            (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
            (source_dir / "short.md").write_text("---\ntitle: Short source\n---\nsmall\n", encoding="utf-8")
            (wiki / "entities").mkdir()
            (wiki / "entities" / "full.md").write_text("x" * 120, encoding="utf-8")
            storage = FakeStorage()
            service = HealthMaintenanceService(
                storage=storage,
                wiki_repo_path=root,
                wiki_lock=threading.RLock(),
            )

            result = service.run(self._job())

            self.assertEqual(result.result_summary["scanned_page_count"], 2)
            self.assertEqual(result.result_summary["empty_or_stub_count"], 1)
            self.assertEqual(result.result_summary["index_difference_count"], 3)
            self.assertEqual(result.result_summary["log_missing_count"], 1)
            self.assertEqual(storage.progress, [("scanning_pages", 20), ("checking_index", 45), ("checking_log", 70), ("writing_report", 90)])
            report = (wiki / "health-report.md").read_text(encoding="utf-8")
            self.assertIn("wiki/sources/short.md", report)
            self.assertIn("wiki/sources/missing.md", report)

    def test_can_skip_report_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "wiki").mkdir()
            storage = FakeStorage()
            service = HealthMaintenanceService(
                storage=storage,
                wiki_repo_path=root,
                wiki_lock=threading.RLock(),
            )

            service.run(self._job(save_report=False))

            self.assertFalse((root / "wiki" / "health-report.md").exists())
            self.assertNotIn(("writing_report", 90), storage.progress)

    def test_shared_agent_parity_fixture_matches_health_oracle_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = create_agent_parity_wiki(root)
            service = HealthMaintenanceService(storage=FakeStorage(), wiki_repo_path=root, wiki_lock=threading.RLock())

            result = service.run(self._job())

            self.assertEqual(result.result_summary["scanned_page_count"], EXPECTED_HEALTH["total_pages"])
            self.assertEqual(result.result_summary["empty_or_stub_count"], len(EXPECTED_HEALTH["empty_paths"]))
            self.assertEqual(result.result_summary["index_difference_count"], 7)
            self.assertEqual(result.result_summary["log_missing_count"], 1)
            report = (wiki / "health-report.md").read_text(encoding="utf-8")
            self.assertIn("## Index Sync", report)
            self.assertIn("## Log Coverage", report)
            for path in (
                EXPECTED_HEALTH["empty_paths"]
                + EXPECTED_HEALTH["stale_index_paths"]
                + EXPECTED_HEALTH["missing_index_paths"]
                + EXPECTED_HEALTH["unlogged_source_paths"]
            ):
                self.assertIn(f"`{path}`", report)
            self.assertLess(report.index("wiki/drafts/empty.md"), report.index("wiki/drafts/short.md"))


if __name__ == "__main__":
    unittest.main()
