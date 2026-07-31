from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path

from app.schemas.maintenance import MaintenanceJobResponse
from app.services.graph_maintenance_service import GraphMaintenanceService
from tests.agent_parity_fixture import EXPECTED_GRAPH, create_agent_parity_wiki


class FakeStorage:
    def __init__(self) -> None:
        self.progress: list[tuple[str, int]] = []

    def update_maintenance_job_progress(self, *, job_id: int, stage: str, progress_percent: int, updated_at: datetime) -> None:
        self.progress.append((stage, progress_percent))


class GraphMaintenanceServiceTests(unittest.TestCase):
    @staticmethod
    def _job() -> MaintenanceJobResponse:
        now = datetime(2026, 7, 29, 11)
        return MaintenanceJobResponse(job_id=1, task_kind="graph", status="running", result_state="unavailable", trigger="manual", stage="starting", progress_percent=5, options={"infer_relations": False, "save_report": True}, result_summary={}, created_at=now, updated_at=now, started_at=now)

    def test_builds_deterministic_graph_from_explicit_wikilinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            (wiki / "entities").mkdir()
            (wiki / "concepts" / "alpha.md").write_text("---\ntype: concept\ntitle: Alpha\n---\nSee [[Beta]].", encoding="utf-8")
            (wiki / "entities" / "beta.md").write_text("---\ntype: entity\n---\nBody", encoding="utf-8")
            storage = FakeStorage()
            service = GraphMaintenanceService(storage=storage, wiki_repo_path=root, wiki_lock=threading.RLock())

            result = service.run(self._job())

            graph = json.loads((root / "graph" / "graph.json").read_text(encoding="utf-8"))
            self.assertEqual(len(graph["nodes"]), 2)
            self.assertEqual(graph["edges"][0]["from"], "concepts/alpha")
            self.assertEqual(graph["edges"][0]["to"], "entities/beta")
            self.assertIn("built", graph)
            self.assertIn("markdown", graph["nodes"][0])
            self.assertIn("group", graph["nodes"][0])
            self.assertEqual(graph["nodes"][0]["value"], 2)
            self.assertIn(result.result_state, {"complete", "partial"})
            self.assertEqual(storage.progress[-1], ("writing_graph", 92))
            self.assertTrue((root / "graph" / "graph-report.md").is_file())
            self.assertTrue((root / "graph" / "graph.html").is_file())
            self.assertIn("Knowledge graph rebuilt", (wiki / "log.md").read_text(encoding="utf-8"))

    def test_can_skip_graph_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            (wiki / "sources").mkdir(parents=True)
            (wiki / "sources" / "page.md").write_text("Body", encoding="utf-8")

            GraphMaintenanceService(storage=FakeStorage(), wiki_repo_path=root, wiki_lock=threading.RLock()).run(
                self._job().model_copy(update={"options": {"infer_relations": False, "save_report": False}})
            )

            self.assertTrue((root / "graph" / "graph.json").is_file())
            self.assertFalse((root / "graph" / "graph-report.md").exists())

    def test_shared_agent_parity_fixture_preserves_markdown_and_communities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_agent_parity_wiki(root)

            GraphMaintenanceService(storage=FakeStorage(), wiki_repo_path=root, wiki_lock=threading.RLock()).run(self._job())

            graph = json.loads((root / "graph" / "graph.json").read_text(encoding="utf-8"))
            alpha = next(node for node in graph["nodes"] if node["id"] == "sources/alpha")
            self.assertEqual(len(graph["nodes"]), EXPECTED_GRAPH["node_count"])
            self.assertEqual({frozenset((edge["from"], edge["to"])) for edge in graph["edges"]}, EXPECTED_GRAPH["edge_pairs"])
            self.assertIn("markdown", alpha)
            self.assertIsInstance(alpha["group"], int)
            self.assertGreaterEqual(alpha["value"], 1)
            self.assertIn(alpha["color"], {"#E91E63", "#00BCD4", "#8BC34A", "#FF5722", "#673AB7", "#FFC107", "#009688", "#F44336", "#3F51B5", "#CDDC39"})
            report = (root / "graph" / "graph-report.md").read_text(encoding="utf-8")
            self.assertIn(f"`[[{EXPECTED_GRAPH['phantom_hub']}]]`", report)
            self.assertIn("## Suggested Actions", report)
            html = (root / "graph" / "graph.html").read_text(encoding="utf-8")
            self.assertIn('id="conf-slider"', html)
            self.assertIn("Number(e.confidence??1)>=s.v", html)
            log = (root / "wiki" / "log.md").read_text(encoding="utf-8")
            self.assertLess(log.index("report | Graph health report generated"), log.index("graph | Knowledge graph rebuilt"))

    def test_inference_failure_keeps_deterministic_graph_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            (wiki / "sources").mkdir(parents=True)
            (wiki / "sources" / "page.md").write_text("content", encoding="utf-8")
            service = GraphMaintenanceService(storage=FakeStorage(), wiki_repo_path=root, wiki_lock=threading.RLock())
            job = self._job().model_copy(update={"options": {"infer_relations": True}})

            with patch("app.services.graph_maintenance_service.call_llm_fast", side_effect=RuntimeError("LLM unavailable")):
                result = service.run(job)

            self.assertEqual(result.result_state, "partial")
            self.assertEqual(result.result_summary["inference_status"], "failed")
            self.assertTrue((root / "graph" / "graph.json").is_file())

    def test_keeps_graph_artifact_when_networkx_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            (wiki / "sources").mkdir(parents=True)
            (wiki / "sources" / "alpha.md").write_text("[[Beta]]", encoding="utf-8")
            (wiki / "sources" / "beta.md").write_text("Body", encoding="utf-8")
            service = GraphMaintenanceService(storage=FakeStorage(), wiki_repo_path=root, wiki_lock=threading.RLock())

            with patch.dict(sys.modules, {"networkx": None}):
                result = service.run(self._job())

            graph = json.loads((root / "graph" / "graph.json").read_text(encoding="utf-8"))
            self.assertEqual(result.result_summary["community_detection"], "unavailable")
            self.assertTrue(all(node["group"] == -1 for node in graph["nodes"]))

    def test_inference_checkpoint_is_reused_and_report_has_agent_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            (wiki / "sources").mkdir(parents=True)
            (wiki / "sources" / "first.md").write_text("[[Second]]", encoding="utf-8")
            (wiki / "sources" / "second.md").write_text("Body", encoding="utf-8")
            service = GraphMaintenanceService(storage=FakeStorage(), wiki_repo_path=root, wiki_lock=threading.RLock())
            job = self._job().model_copy(update={"options": {"infer_relations": True, "save_report": True}})

            with patch("app.services.graph_maintenance_service.call_llm_fast", return_value='{"edges":[{"to":"sources/second","relationship":"related","confidence":0.9}]}') as caller:
                service.run(job)
            with patch("app.services.graph_maintenance_service.call_llm_fast", side_effect=RuntimeError("must use checkpoint")):
                service.run(job)

            self.assertEqual(caller.call_count, 2)
            self.assertTrue(all(call.kwargs == {} for call in caller.call_args_list))
            self.assertTrue((root / "graph" / ".inferred_edges.jsonl").is_file())
            report = (root / "graph" / "graph-report.md").read_text(encoding="utf-8")
            self.assertIn("## 🟡 Fragile Bridges", report)
            self.assertIn("## Suggested Actions", report)
            html = (root / "graph" / "graph.html").read_text(encoding="utf-8")
            self.assertIn('id="conf-slider"', html)
            self.assertIn("function renderMarkdown", html)
            self.assertIn("function clearSelection", html)
            self.assertIn("stabilizationIterationsDone", html)
            self.assertIn(r"split(/\r?\n/)", html)

    def test_excludes_generated_health_report_from_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wiki = root / "wiki"
            (wiki / "sources").mkdir(parents=True)
            (wiki / "sources" / "source.md").write_text("Source body", encoding="utf-8")
            (wiki / "health-report.md").write_text("[[source]]", encoding="utf-8")

            GraphMaintenanceService(storage=FakeStorage(), wiki_repo_path=root, wiki_lock=threading.RLock()).run(self._job())

            graph = json.loads((root / "graph" / "graph.json").read_text(encoding="utf-8"))
            self.assertEqual([node["id"] for node in graph["nodes"]], ["sources/source"])
            self.assertEqual(graph["edges"], [])


if __name__ == "__main__":
    unittest.main()
