from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from app.llm_config import IngestModelCapabilities, IngestModelProfile, LLMProfile
from app.schemas.ingest import IngestJobResponse
from app.services.ingest_large_document_orchestrator import (
    LargeDocumentIngestOrchestrator,
)
from app.services.ingest_large_document_status import (
    LargeDocumentProgressEvent,
    LargeDocumentStageError,
)


def make_profile() -> IngestModelProfile:
    return IngestModelProfile(
        model_identifier="ollama_chat/qwen3.8:27b",
        llm_profile=LLMProfile(
            provider="ollama_chat",
            model="qwen3.8:27b",
            api_key=None,
            api_base="http://127.0.0.1:11434",
            max_tokens=8_192,
            temperature=0.1,
            reasoning_effort="none",
        ),
        capabilities=IngestModelCapabilities(
            max_input_tokens=49_152,
            max_output_tokens=8_192,
            context_window_tokens=65_536,
            context_safety_margin_tokens=8_192,
        ),
    )


def make_job() -> IngestJobResponse:
    created_at = datetime(2026, 10, 20, 9, 0, 0)
    return IngestJobResponse(
        job_id=17,
        status="running",
        stage="extracting",
        progress_percent=35,
        original_filename="rm6-manual.md",
        source_path="raw/uploads/manual/rm6-manual.md",
        ingest_model="ollama_chat/qwen3.8:27b",
        created_at=created_at,
        updated_at=created_at,
    )


class SuccessfulPipelineLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, prompt: str, *, max_tokens: int) -> str:
        self.calls.append((prompt, max_tokens))
        if "Chunk metadata:" in prompt:
            chunk_id = self._required_match(r"- chunk_id: ([^\n]+)", prompt)
            source_line = self._required_match(
                r"- primary source lines: (\d+)-(\d+)",
                prompt,
                group=2,
            )
            return json.dumps(
                {
                    "extraction_status": "succeeded",
                    "extraction_error": None,
                    "chunk_id": chunk_id,
                    "section_title": "技术规格",
                    "summary": "RM6 手册给出额定电压。",
                    "evidence": [
                        {
                            "evidence_id": f"{chunk_id}:e001",
                            "statement": "RM6 的额定电压为 24kV。",
                            "evidence_text": "额定电压为24kV。",
                            "source_lines": source_line,
                            "category": "specification",
                        }
                    ],
                    "entities": ["RM6"],
                    "concepts": ["中压开关柜"],
                    "internal_conflicts": [],
                },
                ensure_ascii=False,
            )
        if "Create one document-level knowledge plan" in prompt:
            chunk_id = self._required_match(r'"chunk_id":"([^"]+)"', prompt)
            evidence_id = f"{chunk_id}:e001"
            return json.dumps(
                {
                    "planning_status": "succeeded",
                    "planning_error": None,
                    "title": "RM6 Manual",
                    "slug": "rm6-manual",
                    "document_summary": "RM6 技术手册。",
                    "key_claims": ["RM6 的额定电压为 24kV。"],
                    "covered_chunk_ids": [chunk_id],
                    "planned_pages": [
                        {
                            "page_type": "source",
                            "title": "RM6 Manual",
                            "path": "sources/rm6-manual.md",
                            "purpose": "保存完整来源知识。",
                            "supporting_chunk_ids": [chunk_id],
                            "supporting_evidence_ids": [evidence_id],
                        }
                    ],
                    "patch_intents": [],
                    "contradictions": [],
                },
                ensure_ascii=False,
            )
        if "Generate the single Source page" in prompt:
            return json.dumps(
                {
                    "generation_status": "succeeded",
                    "generation_error": None,
                    "target_id": "source",
                    "title": "RM6 Manual",
                    "slug": "rm6-manual",
                    "content": (
                        "---\n"
                        'title: "RM6 Manual"\n'
                        "type: source\n"
                        "tags: [RM6]\n"
                        "date: 2026-10-20\n"
                        "---\n\n"
                        "# RM6 Manual\n\n额定电压为24kV。\n"
                    ),
                },
                ensure_ascii=False,
            )
        raise AssertionError("unexpected large-document prompt")

    @staticmethod
    def _required_match(
        pattern: str,
        value: str,
        *,
        group: int = 1,
    ) -> str:
        match = re.search(pattern, value)
        if match is None:
            raise AssertionError(f"pattern not found: {pattern}")
        return match.group(group)


class LargeDocumentIngestOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.wiki_dir = Path(self.temp_dir.name) / "wiki"
        self.wiki_dir.mkdir()
        self.orchestrator = LargeDocumentIngestOrchestrator(
            wiki_dir=self.wiki_dir,
            wiki_lock=threading.RLock(),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_runs_complete_pipeline_and_returns_existing_write_contract(self) -> None:
        caller = SuccessfulPipelineLLM()
        progress: list[LargeDocumentProgressEvent] = []

        result = self.orchestrator.ingest(
            job=make_job(),
            source=Path("rm6-manual.md"),
            source_content="# 技术规格\n\n额定电压为24kV。\n",
            ingest_profile=make_profile(),
            call_llm=caller,
            progress_callback=progress.append,
        )

        self.assertEqual(result.data.slug, "rm6-manual")
        self.assertEqual(result.data.index_entry.count("sources/rm6-manual.md"), 1)
        self.assertEqual(result.selected_page_hashes, {})
        self.assertEqual(len(caller.calls), 3)
        self.assertEqual(
            {event.phase for event in progress},
            {
                "chunking",
                "chunk_extraction",
                "document_reduce",
                "wiki_retrieval",
                "page_generation",
                "result_assembly",
            },
        )

    def test_chunk_failure_stops_before_reduce_and_is_normalized(self) -> None:
        calls = 0

        def fail_first_call(prompt: str, *, max_tokens: int) -> str:
            nonlocal calls
            calls += 1
            raise ConnectionError("model unavailable")

        with self.assertRaises(LargeDocumentStageError) as raised:
            self.orchestrator.ingest(
                job=make_job(),
                source=Path("rm6-manual.md"),
                source_content="# 技术规格\n\n额定电压为24kV。\n",
                ingest_profile=make_profile(),
                call_llm=fail_first_call,
                progress_callback=lambda event: None,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.phase, "chunk_extraction")
        self.assertEqual(raised.exception.category, "chunk_llm_call_failed")


if __name__ == "__main__":
    unittest.main()
