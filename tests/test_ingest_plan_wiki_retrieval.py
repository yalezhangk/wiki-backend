from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import TracebackType
from unittest.mock import patch

from app.schemas.ingest_large_document import IngestKnowledgePlan
from app.services.ingest_context import WikiSnapshot
from app.services.ingest_large_document_status import LargeDocumentProgressEvent
from app.services.ingest_plan_wiki_retrieval import KnowledgePlanWikiRetriever


class RecordingLock:
    def __init__(self) -> None:
        self.active = False
        self.enter_count = 0

    def __enter__(self) -> object:
        self.active = True
        self.enter_count += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.active = False


def make_plan() -> IngestKnowledgePlan:
    return IngestKnowledgePlan.model_validate(
        {
            "planning_status": "succeeded",
            "planning_error": None,
            "title": "RM6 SeT 技术手册",
            "slug": "rm6-set-technical-manual",
            "document_summary": "不应进入检索词的完整文档摘要。",
            "key_claims": [
                "额定电压为 24kV。",
                "额定电压为 24kV。",
            ],
            "covered_chunk_ids": ["chunk-0001-hash"],
            "planned_pages": [
                {
                    "page_type": "source",
                    "title": "RM6 SeT 技术手册",
                    "path": "sources/rm6-set-technical-manual.md",
                    "purpose": "保存完整来源",
                    "supporting_chunk_ids": ["chunk-0001-hash"],
                    "supporting_evidence_ids": ["chunk-0001-hash:e001"],
                },
                {
                    "page_type": "entity",
                    "title": "RM6 SeT",
                    "path": "entities/rm6-set.md",
                    "purpose": "记录气箱材料",
                    "supporting_chunk_ids": ["chunk-0001-hash"],
                    "supporting_evidence_ids": ["chunk-0001-hash:e001"],
                },
                {
                    "page_type": "concept",
                    "title": "中压开关柜",
                    "path": "concepts/medium-voltage-switchgear.md",
                    "purpose": "记录绝缘要求",
                    "supporting_chunk_ids": ["chunk-0001-hash"],
                    "supporting_evidence_ids": ["chunk-0001-hash:e001"],
                },
            ],
            "patch_intents": [],
            "contradictions": [],
        }
    )


class KnowledgePlanWikiRetrieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.agent_root = Path(self.temp_dir.name)
        self.wiki_dir = self.agent_root / "wiki"
        self.wiki_dir.mkdir()
        self.lock = RecordingLock()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_page(self, relative_path: str, content: str) -> Path:
        path = self.wiki_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_query_uses_only_knowledge_plan_retrieval_fields(self) -> None:
        query = KnowledgePlanWikiRetriever.build_query(make_plan())

        self.assertIn("RM6 SeT 技术手册", query)
        self.assertEqual(query.count("额定电压为 24kV。"), 1)
        self.assertIn("[[RM6 SeT]]", query)
        self.assertIn("[[中压开关柜]]", query)
        self.assertIn("记录气箱材料", query)
        self.assertNotIn("不应进入检索词的完整文档摘要", query)
        self.assertNotIn("chunk-0001-hash", query)
        self.assertNotIn("e001", query)

    def test_retrieves_relevant_page_types_and_snapshot_hashes(self) -> None:
        pages = {
            "overview.md": "# Overview\n\n## RM6 SeT\nRM6 SeT 产品总览。\n",
            "sources/existing-manual.md": (
                "# Existing manual\n\n## 额定电压为 24kV\n来源记录。\n"
            ),
            "entities/rm6-set.md": "# RM6 SeT\n\n## 气箱材料\n采用不锈钢。\n",
            "concepts/medium-voltage-switchgear.md": (
                "# 中压开关柜\n\n## 绝缘要求\n说明绝缘要求。\n"
            ),
            "sources/unrelated.md": "# Astronomy\n\n## Telescope\nOptical telescope.\n",
        }
        for relative_path, content in pages.items():
            self._write_page(relative_path, content)
        retriever = KnowledgePlanWikiRetriever(
            wiki_dir=self.wiki_dir,
            wiki_lock=self.lock,
        )

        result = retriever.retrieve(make_plan(), job_id=31)

        selected_types = {candidate.page_type for candidate in result.selected}
        self.assertEqual(
            selected_types,
            {"overview", "sources", "entities", "concepts"},
        )
        self.assertNotIn("Optical telescope", result.rendered_context)
        self.assertEqual(
            result.selected_page_hashes["entities/rm6-set.md"],
            hashlib.sha256(pages["entities/rm6-set.md"].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            result.selected_page_hashes["overview.md"],
            hashlib.sha256(pages["overview.md"].encode("utf-8")).hexdigest(),
        )

    def test_snapshot_is_captured_while_wiki_lock_is_held(self) -> None:
        self._write_page("overview.md", "# Overview\n\n## RM6 SeT\nRelevant.\n")
        original_capture = WikiSnapshot.capture

        def checked_capture(wiki_dir: Path) -> WikiSnapshot:
            self.assertTrue(self.lock.active)
            return original_capture(wiki_dir)

        retriever = KnowledgePlanWikiRetriever(
            wiki_dir=self.wiki_dir,
            wiki_lock=self.lock,
        )
        with patch(
            "app.services.ingest_plan_wiki_retrieval.WikiSnapshot.capture",
            side_effect=checked_capture,
        ):
            retriever.retrieve(make_plan(), job_id=32)

        self.assertEqual(self.lock.enter_count, 1)
        self.assertFalse(self.lock.active)

    def test_zero_budget_returns_explicit_empty_context(self) -> None:
        self._write_page("overview.md", "# Overview\n\n## RM6 SeT\nRelevant.\n")
        retriever = KnowledgePlanWikiRetriever(
            wiki_dir=self.wiki_dir,
            wiki_lock=self.lock,
            max_context_tokens=0,
        )
        progress: list[LargeDocumentProgressEvent] = []

        result = retriever.retrieve(
            make_plan(),
            job_id=33,
            progress_callback=progress.append,
        )

        self.assertEqual(result.selected, ())
        self.assertEqual(result.skipped, ("budget_zero",))
        self.assertEqual(result.selected_page_hashes, {})
        self.assertEqual(
            result.rendered_context,
            "(no relevant existing Wiki evidence was retrieved)",
        )
        self.assertEqual(len(progress), 2)
        self.assertEqual(
            [event.phase for event in progress],
            ["wiki_retrieval", "wiki_retrieval"],
        )
        self.assertEqual(
            [event.progress_percent for event in progress],
            [55, 57],
        )

    def test_retrieval_is_deterministic_for_same_snapshot(self) -> None:
        self._write_page("overview.md", "# Overview\n\n## RM6 SeT\nRelevant.\n")
        self._write_page(
            "entities/rm6-set.md",
            "# RM6 SeT\n\n## 气箱材料\n采用不锈钢。\n",
        )
        retriever = KnowledgePlanWikiRetriever(
            wiki_dir=self.wiki_dir,
            wiki_lock=self.lock,
        )

        first = retriever.retrieve(make_plan(), job_id=34)
        second = retriever.retrieve(make_plan(), job_id=34)

        self.assertEqual(first, second)

    def test_existing_alias_page_is_prioritized_before_global_budget(self) -> None:
        self._write_page(
            "sources/high-score.md",
            "# RM6 SeT\n\n## 额定电压为 24kV\n" + "相关来源。" * 120,
        )
        entity_content = "# RM6SeT\n\n## Key Details\n既有 RM6 SeT 实体页。\n"
        self._write_page("entities/RM6SeT.md", entity_content)
        retriever = KnowledgePlanWikiRetriever(
            wiki_dir=self.wiki_dir,
            wiki_lock=self.lock,
            max_context_tokens=80,
        )

        result = retriever.retrieve(make_plan(), job_id=35)

        self.assertIn("entities/RM6SeT.md", result.selected_page_hashes)
        self.assertEqual(
            result.selected[0].path,
            "wiki/entities/RM6SeT.md",
        )

    def test_negative_budget_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            KnowledgePlanWikiRetriever(
                wiki_dir=self.wiki_dir,
                wiki_lock=self.lock,
                max_context_tokens=-1,
            )


if __name__ == "__main__":
    unittest.main()
