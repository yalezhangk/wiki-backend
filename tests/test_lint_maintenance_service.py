from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.schemas.maintenance import MaintenanceJobResponse
from app.services.lint_maintenance_service import LintMaintenanceService
from tests.agent_parity_fixture import EXPECTED_LINT, create_agent_parity_wiki


class FakeStorage:
    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.findings: list[dict[str, object]] = []

    def update_maintenance_job_progress(self, **_: object) -> None: pass
    def upsert_maintenance_page_states(self, *, page_hashes: dict[str, str], checked_at: datetime) -> None: self.states = page_hashes
    def get_maintenance_page_states(self) -> dict[str, dict[str, object]]: return {}
    def mark_maintenance_pages_semantically_checked(self, **_: object) -> None: pass
    def replace_maintenance_findings(self, *, job_id: int, findings: list[dict[str, object]], created_at: datetime) -> None: self.findings = findings


class LintMaintenanceServiceTests(unittest.TestCase):
    def test_semantic_response_normalizes_common_confidence_labels(self) -> None:
        parsed = LintMaintenanceService._parse_semantic_response(
            """{
                "contradictions": [],
                "stale_content": [],
                "data_gaps": [{
                    "pages": ["page.md"],
                    "evidence": "Gap",
                    "recommendation": "Add source",
                    "confidence": "high"
                }],
                "concepts_needing_depth": [{
                    "pages": ["page.md"],
                    "evidence": "Thin",
                    "recommendation": "Expand it",
                    "confidence": "medium"
                }]
            }"""
        )

        self.assertEqual(parsed.data_gaps[0].confidence, 0.9)
        self.assertEqual(parsed.concepts_needing_depth[0].confidence, 0.6)

    def test_semantic_response_normalizes_single_page_string(self) -> None:
        parsed = LintMaintenanceService._parse_semantic_response(
            """{
                "contradictions": [],
                "stale_content": [{
                    "pages": "concepts/智能真空断路器.md",
                    "evidence": "Outdated",
                    "recommendation": "Refresh it",
                    "confidence": 0.9
                }],
                "data_gaps": [],
                "concepts_needing_depth": []
            }"""
        )

        self.assertEqual(
            parsed.stale_content[0].pages,
            ["concepts/智能真空断路器.md"],
        )

    def test_semantic_response_extracts_json_code_block(self) -> None:
        fence = chr(96) * 3
        parsed = LintMaintenanceService._parse_semantic_response(
            f"Model analysis:\\n{fence}json\\n"
            '{"contradictions":[],"stale_content":[],"data_gaps":[],"concepts_needing_depth":[]}'
            f"\\n{fence}"
        )

        self.assertEqual(parsed.contradictions, [])

    def test_delta_preserves_unstructured_markdown_as_partial_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            wiki.mkdir()
            (wiki / "page.md").write_text("Content", encoding="utf-8")
            now = datetime(2026, 7, 30, 12)
            job = MaintenanceJobResponse(
                job_id=5,
                task_kind="lint",
                status="running",
                result_state="unavailable",
                trigger="manual",
                stage="starting",
                progress_percent=5,
                options={"semantic_analysis": True, "semantic_mode": "delta"},
                result_summary={},
                created_at=now,
                updated_at=now,
            )
            raw_report = "## Data Gaps & Suggested Sources\\n\\n- Add source coverage."

            with patch(
                "app.services.lint_maintenance_service.call_llm_main",
                return_value=raw_report,
            ):
                result = LintMaintenanceService(
                    storage=FakeStorage(),
                    wiki_repo_path=root,
                    wiki_lock=threading.RLock(),
                ).run(job)

            self.assertEqual(result.result_state, "partial")
            self.assertEqual(result.result_summary["semantic_status"], "unstructured")
            self.assertIn(raw_report, (wiki / "lint-report.md").read_text(encoding="utf-8"))

    def test_deterministic_checks_persist_hashes_and_findings_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            wiki.mkdir()
            (wiki / "a.md").write_text("[[Missing]]", encoding="utf-8")
            (wiki / "b.md").write_text("[[Missing]]", encoding="utf-8")
            (wiki / "c.md").write_text("[[Missing]]", encoding="utf-8")
            storage = FakeStorage()
            now = datetime(2026, 7, 29, 12)
            job = MaintenanceJobResponse(job_id=1, task_kind="lint", status="running", result_state="unavailable", trigger="manual", stage="starting", progress_percent=5, options={"semantic_analysis": False}, result_summary={}, created_at=now, updated_at=now)

            result = LintMaintenanceService(storage=storage, wiki_repo_path=root, wiki_lock=threading.RLock()).run(job)

            self.assertEqual(result.result_state, "complete")
            self.assertEqual(result.result_summary["semantic_status"], "not_run")
            self.assertEqual(len(storage.states), 3)
            self.assertTrue(any(item["finding_type"] == "missing_entity" for item in storage.findings))
            report = (wiki / "lint-report.md").read_text(encoding="utf-8")
            self.assertIn("# Wiki Lint Report —", report)
            self.assertIn("### Broken Wikilinks", report)
            self.assertIn("### Missing Entity Pages", report)
            self.assertIn("lint | Wiki health check", (wiki / "log.md").read_text(encoding="utf-8"))

    def test_index_link_is_navigation_but_not_graph_inbound_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            source_dir = wiki / "sources"
            entity_dir = wiki / "entities"
            source_dir.mkdir(parents=True)
            entity_dir.mkdir()
            (wiki / "index.md").write_text("[Source](sources/source.md)\n", encoding="utf-8")
            (source_dir / "source.md").write_text("[[entities/target#details|Target]]", encoding="utf-8")
            (entity_dir / "target.md").write_text("Target content", encoding="utf-8")
            (wiki / "health-report.md").write_text("Generated report", encoding="utf-8")
            now = datetime(2026, 7, 30, 12)
            job = MaintenanceJobResponse(job_id=6, task_kind="lint", status="running", result_state="unavailable", trigger="manual", stage="starting", progress_percent=5, options={"semantic_analysis": False}, result_summary={}, created_at=now, updated_at=now)
            storage = FakeStorage()

            result = LintMaintenanceService(storage=storage, wiki_repo_path=root, wiki_lock=threading.RLock()).run(job)

            warning_orphans = {item["affected_pages"][0] for item in storage.findings if item["finding_type"] == "orphan"}
            graph_orphans = {item["affected_pages"][0] for item in storage.findings if item["finding_type"] == "graph_orphan"}
            broken_links = [item for item in storage.findings if item["finding_type"] == "broken_link"]
            self.assertNotIn("sources/source", warning_orphans)
            self.assertIn("sources/source", graph_orphans)
            self.assertNotIn("entities/target", warning_orphans)
            self.assertEqual(broken_links, [])
            self.assertNotIn("health-report", storage.states)
            self.assertEqual(result.result_summary["graph_link_orphan_count"], 1)
            self.assertIn("### Graph-Link Orphans", (wiki / "lint-report.md").read_text(encoding="utf-8"))

    def test_shared_agent_parity_fixture_is_accepted_by_lint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_agent_parity_wiki(root)
            now = datetime(2026, 7, 30, 12)
            job = MaintenanceJobResponse(job_id=3, task_kind="lint", status="running", result_state="unavailable", trigger="manual", stage="starting", progress_percent=5, options={"semantic_analysis": False}, result_summary={}, created_at=now, updated_at=now)

            storage = FakeStorage()
            result = LintMaintenanceService(storage=storage, wiki_repo_path=root, wiki_lock=threading.RLock()).run(job)

            self.assertEqual(result.result_state, "complete")
            report = (root / "wiki" / "lint-report.md").read_text(encoding="utf-8")
            self.assertEqual({item["affected_pages"][0] for item in storage.findings if item["finding_type"] == "orphan"}, EXPECTED_LINT["orphan_pages"])
            broken_pages = {page for item in storage.findings if item["finding_type"] == "broken_link" for page in item["affected_pages"]}
            self.assertEqual(broken_pages, EXPECTED_LINT["broken_link_pages"])
            self.assertIn(f"`[[{EXPECTED_LINT['missing_entity']}]]`", report)
            self.assertIn("### Sparse Pages — Low Outbound Link Density (2 pages)", report)
            self.assertIn("[!tip]", report)

    def test_agent_compat_failure_keeps_deterministic_report_and_audit_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_agent_parity_wiki(root)
            now = datetime(2026, 7, 30, 12)
            job = MaintenanceJobResponse(job_id=4, task_kind="lint", status="running", result_state="unavailable", trigger="manual", stage="starting", progress_percent=5, options={"semantic_analysis": True, "semantic_mode": "agent_compat"}, result_summary={}, created_at=now, updated_at=now)

            with patch("app.services.lint_maintenance_service.call_llm_main", side_effect=RuntimeError("provider unavailable")):
                result = LintMaintenanceService(storage=FakeStorage(), wiki_repo_path=root, wiki_lock=threading.RLock()).run(job)

            report = (root / "wiki" / "lint-report.md").read_text(encoding="utf-8")
            self.assertEqual(result.result_state, "partial")
            self.assertEqual(result.result_summary["semantic_status"], "unavailable")
            self.assertIn("Semantic analysis unavailable.", report)
            audit = result.result_summary["semantic_report_audit"]
            self.assertEqual(audit["report_char_count"], len("Semantic analysis unavailable."))
            self.assertEqual(len(audit["report_sha256"]), 64)

    def test_comparison_groups_keep_linked_pages_together(self) -> None:
        root = Path("C:/temporary/wiki")
        paths = {"a.md": root / "a.md", "b.md": root / "b.md", "c.md": root / "c.md"}
        content = {paths["a.md"]: "[[b]]", paths["b.md"]: "", paths["c.md"]: ""}

        groups = LintMaintenanceService._comparison_groups(["a.md", "b.md", "c.md"], paths, content)

        self.assertEqual(groups[0], ["a.md", "b.md"])
        self.assertEqual(groups[1], ["c.md"])

    def test_agent_compat_uses_agent_sample_and_markdown_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            wiki.mkdir()
            for index in range(21):
                (wiki / f"page-{index:02}.md").write_text("x" * 1600, encoding="utf-8")
            now = datetime(2026, 7, 30, 12)
            job = MaintenanceJobResponse(job_id=2, task_kind="lint", status="running", result_state="unavailable", trigger="manual", stage="starting", progress_percent=5, options={"semantic_analysis": True, "semantic_mode": "agent_compat"}, result_summary={}, created_at=now, updated_at=now)

            with patch("app.services.lint_maintenance_service.call_llm_main", return_value="## Contradictions\n\nNone.") as caller:
                result = LintMaintenanceService(storage=FakeStorage(), wiki_repo_path=root, wiki_lock=threading.RLock()).run(job)

            self.assertEqual(caller.call_args.kwargs, {})
            prompt = caller.call_args.args[0]
            self.assertIn("sample of 20 pages", prompt)
            self.assertIn("x" * 1500, prompt)
            self.assertNotIn("page-20.md", prompt)
            self.assertEqual(result.result_summary["semantic_coverage"]["checked_page_count"], 20)
            self.assertIn("## Contradictions", (wiki / "lint-report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
