from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.maintenance import MaintenanceJobResponse
from app.services.quality_report_service import QualityReportService


class FakeMaintenanceStorage:
    def __init__(
        self,
        job: MaintenanceJobResponse | None = None,
        findings: list[dict[str, object]] | None = None,
    ) -> None:
        self._job = job
        self._findings = findings or []

    def list_maintenance_jobs(self, **_: object) -> list[MaintenanceJobResponse]:
        return [self._job] if self._job is not None else []

    def list_maintenance_findings(self, *, job_id: int) -> list[dict[str, object]]:
        return self._findings if self._job is not None and job_id == self._job.job_id else []


class QualityReportServiceTests(unittest.TestCase):
    def test_missing_reports_are_explicit_and_api_never_runs_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "wiki").mkdir()
            (root / "wiki" / "page.md").write_text("body", encoding="utf-8")
            service = QualityReportService(wiki_repo_path=root, stale_after_hours=24)

            result = service.get_latest()

            self.assertEqual(result.snapshot.status, "missing")
            self.assertEqual(result.snapshot.checks["health"].state, "missing")
            self.assertEqual(result.snapshot.current_object_count, 1)

    def test_reports_are_parsed_without_absolute_path_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            graph = root / "graph"
            wiki.mkdir()
            graph.mkdir()
            (wiki / "page.md").write_text("body", encoding="utf-8")
            (wiki / "health-report.md").write_text("## Empty / Stub Files (2 found)\n## Index Sync (3 issues)\n## Log Coverage (1 source pages without log entry)\n", encoding="utf-8")
            (wiki / "lint-report.md").write_text("# Lint\n- This Markdown line must not become a finding.\n", encoding="utf-8")
            (graph / "graph-report.md").write_text("- Orphan nodes: 1\n- Hub nodes: 2\n", encoding="utf-8")
            now = datetime(2026, 7, 31, 12)
            lint_job = MaintenanceJobResponse(job_id=42, task_kind="lint", status="succeeded", result_state="complete", trigger="manual", stage="completed", progress_percent=100, options={}, result_summary={"semantic_coverage": {"checked_page_count": 2}}, created_at=now, updated_at=now, finished_at=now)
            storage = FakeMaintenanceStorage(
                job=lint_job,
                findings=[
                    {"finding_id": 1, "finding_type": "orphan", "severity": "warning", "affected_pages": ["sources/a"], "evidence": [{"quote": "No inbound links."}], "recommendation": "Add an index link.", "review_status": "needs_review"},
                    {"finding_id": 2, "finding_type": "missing_entity", "severity": "warning", "affected_pages": ["entities/example"], "evidence": [], "recommendation": "Create the entity page.", "review_status": "needs_review"},
                    {"finding_id": 3, "finding_type": "graph_orphan", "severity": "info", "affected_pages": ["sources/a"], "evidence": [], "recommendation": "Add a WikiLink if graph coverage is needed.", "review_status": "needs_review"},
                    {"finding_id": 4, "finding_type": "contradiction", "severity": "warning", "affected_pages": ["concepts/a", "concepts/b"], "evidence": [{"quote": "The two claims conflict."}], "recommendation": "Verify both sources.", "review_status": "needs_review"},
                    {"finding_id": 5, "finding_type": "data_gap", "severity": "warning", "affected_pages": ["concepts/a"], "evidence": [], "recommendation": "Add a primary source.", "review_status": "needs_review"},
                ],
            )
            service = QualityReportService(wiki_repo_path=root, stale_after_hours=24, maintenance_storage=storage)

            result = service.get_latest()

            self.assertEqual(result.snapshot.status, "available")
            self.assertEqual(result.structural.checks[1]["count"], 3)
            self.assertEqual(result.tab_counts, {"all": 7, "consistency": 2, "structure": 2, "graph": 3, "freshness": 0})
            self.assertEqual(result.structural.findings[0].pages, ["sources/a"])
            self.assertEqual(result.structural.findings[0].evidence[0]["quote"], "No inbound links.")
            self.assertEqual(result.consistency["findings"][0].title, "内容矛盾：concepts/a、concepts/b")
            self.assertEqual(result.graph["findings"][0].category, "graph")
            self.assertNotIn(str(root), result.model_dump_json())

    def test_stale_report_is_not_presented_as_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            wiki.mkdir()
            report = wiki / "health-report.md"
            report.write_text("# old", encoding="utf-8")
            old_timestamp = 1_700_000_000
            os.utime(report, (old_timestamp, old_timestamp))

            result = QualityReportService(wiki_repo_path=root, stale_after_hours=1).get_latest()

            self.assertEqual(result.snapshot.checks["health"].state, "stale")

    def test_agent_style_graph_report_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            graph = root / "graph"
            wiki.mkdir()
            graph.mkdir()
            (wiki / "page.md").write_text("body", encoding="utf-8")
            (graph / "graph-report.md").write_text("## 🔴 Orphan Nodes (2 pages, 50.0%)\n## 🟠 Phantom Hubs (referenced but non-existent pages) (1)\n", encoding="utf-8")

            result = QualityReportService(wiki_repo_path=root, stale_after_hours=24).get_latest()

            self.assertEqual(len(result.graph["findings"]), 2)

    def test_api_returns_503_only_when_wiki_root_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app = create_app(initialize_storage=False)
            app.state.quality_report_service = QualityReportService(wiki_repo_path=root, stale_after_hours=24)

            response = TestClient(app).get("/api/quality/latest")

            self.assertEqual(response.status_code, 503)

    def test_openapi_documents_quality_as_read_only_snapshot(self) -> None:
        document = TestClient(create_app(initialize_storage=False)).get("/openapi.json").json()
        latest = document["paths"]["/api/quality/latest"]["get"]
        schema = document["components"]["schemas"]["QualityResponse"]

        self.assertIn("只读", latest["description"])
        self.assertIn("不会运行巡检", latest["description"])
        self.assertIn("总体状态", schema["properties"]["snapshot"]["description"])


if __name__ == "__main__":
    unittest.main()
