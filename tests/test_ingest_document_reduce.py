from __future__ import annotations

import json
import unittest
from datetime import date

from app.llm_config import IngestModelCapabilities, IngestModelProfile, LLMProfile
from app.schemas.ingest_large_document import IngestChunkExtraction
from app.services.ingest_document_reduce import (
    DocumentKnowledgeReducer,
    DocumentReduceError,
)
from app.services.ingest_large_document_status import LargeDocumentProgressEvent


class FakeReduceLLMCaller:
    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, prompt: str, *, max_tokens: int) -> str:
        self.calls.append((prompt, max_tokens))
        if not self._responses:
            raise AssertionError("unexpected Document Reduce LLM call")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_profile(
    *,
    max_input_tokens: int = 49_152,
    max_output_tokens: int = 8_192,
) -> IngestModelProfile:
    return IngestModelProfile(
        model_identifier="ollama_chat/qwen3.8:27b",
        llm_profile=LLMProfile(
            provider="ollama_chat",
            model="qwen3.8:27b",
            api_key=None,
            api_base="http://127.0.0.1:11434",
            max_tokens=max_output_tokens,
            temperature=0.1,
            reasoning_effort="none",
        ),
        capabilities=IngestModelCapabilities(
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            context_window_tokens=65_536,
            context_safety_margin_tokens=8_192,
        ),
    )


def make_extraction(
    number: int,
    *,
    summary_suffix: str = "",
) -> IngestChunkExtraction:
    chunk_id = f"chunk-{number:04d}-hash{number:04d}"
    return IngestChunkExtraction.model_validate(
        {
            "extraction_status": "succeeded",
            "extraction_error": None,
            "chunk_id": chunk_id,
            "section_title": f"章节 {number}",
            "summary": f"第 {number} 节摘要。{summary_suffix}",
            "evidence": [
                {
                    "evidence_id": f"{chunk_id}:e001",
                    "statement": f"第 {number} 节额定值为 {number}kV。",
                    "evidence_text": f"额定值为 {number}kV",
                    "source_lines": str(number),
                    "category": "specification",
                }
            ],
            "entities": ["RM6 SeT"],
            "concepts": ["中压开关柜"],
            "internal_conflicts": [],
        }
    )


def plan_payload(
    extractions: tuple[IngestChunkExtraction, ...],
    *,
    planned_pages: list[dict[str, object]] | None = None,
    covered_chunk_ids: list[str] | None = None,
    patch_intents: list[dict[str, object]] | None = None,
) -> str:
    first = extractions[0]
    default_pages: list[dict[str, object]] = [
        {
            "page_type": "source",
            "title": "RM6 技术手册",
            "path": "sources/rm6-technical-manual.md",
            "purpose": "记录完整来源",
            "supporting_chunk_ids": [first.chunk_id],
            "supporting_evidence_ids": [first.evidence[0].evidence_id],
        }
    ]
    return json.dumps(
        {
            "planning_status": "succeeded",
            "planning_error": None,
            "title": "RM6 技术手册",
            "slug": "rm6-technical-manual",
            "document_summary": "文档汇总 RM6 技术规格。",
            "key_claims": ["RM6 包含明确的额定参数。"],
            "covered_chunk_ids": covered_chunk_ids
            if covered_chunk_ids is not None
            else [item.chunk_id for item in extractions],
            "planned_pages": planned_pages
            if planned_pages is not None
            else default_pages,
            "patch_intents": patch_intents or [],
            "contradictions": [],
        },
        ensure_ascii=False,
    )


def section_payload(
    section_id: str,
    extractions: tuple[IngestChunkExtraction, ...],
) -> str:
    return json.dumps(
        {
            "synthesis_status": "succeeded",
            "synthesis_error": None,
            "section_id": section_id,
            "covered_chunk_ids": [item.chunk_id for item in extractions],
            "summary": "相邻章节的紧凑归并。",
            "key_claims": [
                {
                    "statement": extractions[0].evidence[0].statement,
                    "supporting_evidence_ids": [
                        extractions[0].evidence[0].evidence_id
                    ],
                }
            ],
            "entities": ["RM6 SeT"],
            "concepts": ["中压开关柜"],
            "internal_conflicts": [],
        },
        ensure_ascii=False,
    )


class DocumentKnowledgeReducerTests(unittest.TestCase):
    def test_direct_reduce_uses_only_structured_extractions(self) -> None:
        extractions = (make_extraction(1), make_extraction(2))
        caller = FakeReduceLLMCaller([plan_payload(extractions)])
        reducer = DocumentKnowledgeReducer(
            ingest_profile=make_profile(),
            call_llm=caller,
        )
        progress: list[LargeDocumentProgressEvent] = []

        result = reducer.reduce(
            extractions,
            document_filename="manual.md",
            today=date(2026, 10, 19),
            job_id=17,
            progress_callback=progress.append,
        )

        self.assertEqual(result.slug, "rm6-technical-manual")
        self.assertEqual(len(caller.calls), 1)
        self.assertIn(extractions[0].evidence[0].statement, caller.calls[0][0])
        self.assertNotIn("PRIMARY SOURCE", caller.calls[0][0])
        self.assertNotIn("Current Wiki state", caller.calls[0][0])
        self.assertIn("at most 6 planned_pages", caller.calls[0][0])
        self.assertEqual(caller.calls[0][1], 5_120)
        self.assertEqual(
            [
                (event.phase, event.completed, event.total, event.item_id)
                for event in progress
            ],
            [
                ("document_reduce", 0, 1, "document-plan"),
                ("document_reduce", 1, 1, "document-plan"),
            ],
        )

    def test_chunk_coverage_must_be_complete_unique_and_known(self) -> None:
        extractions = (make_extraction(1), make_extraction(2))
        invalid_coverages = (
            [extractions[0].chunk_id],
            [extractions[0].chunk_id, extractions[0].chunk_id],
            [extractions[0].chunk_id, "chunk-9999-unknown"],
        )
        for coverage in invalid_coverages:
            with self.subTest(coverage=coverage):
                caller = FakeReduceLLMCaller(
                    [plan_payload(extractions, covered_chunk_ids=coverage)]
                )
                reducer = DocumentKnowledgeReducer(
                    ingest_profile=make_profile(),
                    call_llm=caller,
                )

                with self.assertRaises(DocumentReduceError) as context:
                    reducer.reduce(
                        extractions,
                        document_filename="manual.md",
                        today=date(2026, 10, 19),
                        job_id=18,
                    )

                self.assertEqual(
                    context.exception.category,
                    "reduce_chunk_coverage_invalid",
                )

    def test_page_evidence_must_exist_and_belong_to_supporting_chunks(self) -> None:
        extractions = (make_extraction(1), make_extraction(2))
        page = {
            "page_type": "source",
            "title": "RM6 技术手册",
            "path": "sources/rm6-technical-manual.md",
            "purpose": "记录完整来源",
            "supporting_chunk_ids": [extractions[1].chunk_id],
            "supporting_evidence_ids": [extractions[0].evidence[0].evidence_id],
        }
        caller = FakeReduceLLMCaller(
            [plan_payload(extractions, planned_pages=[page])]
        )
        reducer = DocumentKnowledgeReducer(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(DocumentReduceError) as context:
            reducer.reduce(
                extractions,
                document_filename="manual.md",
                today=date(2026, 10, 19),
                job_id=19,
            )

        self.assertEqual(
            context.exception.category,
            "reduce_supporting_evidence_mismatch",
        )

    def test_duplicate_entity_plan_is_rejected(self) -> None:
        extractions = (make_extraction(1),)
        source_page = json.loads(plan_payload(extractions))["planned_pages"][0]
        entity_page = {
            "page_type": "entity",
            "title": "RM6 SeT",
            "path": "entities/rm6-set.md",
            "purpose": "沉淀产品知识",
            "supporting_chunk_ids": [extractions[0].chunk_id],
            "supporting_evidence_ids": [extractions[0].evidence[0].evidence_id],
        }
        duplicate = {**entity_page, "path": "entities/rm6-set-alias.md"}
        caller = FakeReduceLLMCaller(
            [
                plan_payload(
                    extractions,
                    planned_pages=[source_page, entity_page, duplicate],
                )
            ]
        )
        reducer = DocumentKnowledgeReducer(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(DocumentReduceError) as context:
            reducer.reduce(
                extractions,
                document_filename="manual.md",
                today=date(2026, 10, 19),
                job_id=20,
            )

        self.assertEqual(context.exception.category, "reduce_page_duplicate")

    def test_reduce_cannot_guess_patch_intents_before_wiki_retrieval(self) -> None:
        extractions = (make_extraction(1),)
        patch = {
            "page_type": "entity",
            "existing_path": "entities/rm6-set.md",
            "purpose": "更新规格",
            "supporting_chunk_ids": [extractions[0].chunk_id],
            "supporting_evidence_ids": [extractions[0].evidence[0].evidence_id],
        }
        caller = FakeReduceLLMCaller(
            [plan_payload(extractions, patch_intents=[patch])]
        )
        reducer = DocumentKnowledgeReducer(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(DocumentReduceError) as context:
            reducer.reduce(
                extractions,
                document_filename="manual.md",
                today=date(2026, 10, 19),
                job_id=21,
            )

        self.assertEqual(
            context.exception.category,
            "reduce_patch_intents_premature",
        )

    def test_invalid_json_is_repaired_once_without_structured_input(self) -> None:
        extractions = (make_extraction(1),)
        caller = FakeReduceLLMCaller(["not json", plan_payload(extractions)])
        reducer = DocumentKnowledgeReducer(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        result = reducer.reduce(
            extractions,
            document_filename="manual.md",
            today=date(2026, 10, 19),
            job_id=22,
        )

        self.assertEqual(result.title, "RM6 技术手册")
        self.assertEqual(len(caller.calls), 2)
        self.assertNotIn(extractions[0].evidence[0].statement, caller.calls[1][0])
        self.assertIn("not json", caller.calls[1][0])
        self.assertIn("6 planned_pages", caller.calls[1][0])
        self.assertIn("retain every covered_chunk_id", caller.calls[1][0])

    def test_over_budget_direct_reduce_uses_fixed_fan_in_hierarchy(self) -> None:
        extractions = tuple(
            make_extraction(number, summary_suffix="规" * 700)
            for number in range(1, 9)
        )
        first_group = extractions[:4]
        second_group = extractions[4:]
        source_page = {
            "page_type": "source",
            "title": "RM6 技术手册",
            "path": "sources/rm6-technical-manual.md",
            "purpose": "记录完整来源",
            "supporting_chunk_ids": [
                first_group[0].chunk_id,
                second_group[0].chunk_id,
            ],
            "supporting_evidence_ids": [
                first_group[0].evidence[0].evidence_id,
                second_group[0].evidence[0].evidence_id,
            ],
        }
        caller = FakeReduceLLMCaller(
            [
                section_payload("section-l01-0001", first_group),
                section_payload("section-l01-0002", second_group),
                plan_payload(extractions, planned_pages=[source_page]),
            ]
        )
        reducer = DocumentKnowledgeReducer(
            ingest_profile=make_profile(max_input_tokens=5_500),
            call_llm=caller,
            section_max_output_tokens=2_048,
        )

        result = reducer.reduce(
            extractions,
            document_filename="manual.md",
            today=date(2026, 10, 19),
            job_id=23,
        )

        self.assertEqual(len(result.covered_chunk_ids), 8)
        self.assertEqual(len(caller.calls), 3)
        self.assertIn("section-l01-0001", caller.calls[0][0])
        self.assertIn("section-l01-0002", caller.calls[1][0])
        self.assertIn("section_syntheses", caller.calls[2][0])
        self.assertEqual(
            [max_tokens for _, max_tokens in caller.calls],
            [2_048, 2_048, 5_120],
        )

    def test_invalid_section_coverage_stops_before_final_reduce(self) -> None:
        extractions = tuple(
            make_extraction(number, summary_suffix="规" * 700)
            for number in range(1, 9)
        )
        invalid_section = json.loads(
            section_payload("section-l01-0001", extractions[:4])
        )
        invalid_section["covered_chunk_ids"] = [extractions[0].chunk_id]
        caller = FakeReduceLLMCaller(
            [json.dumps(invalid_section, ensure_ascii=False)]
        )
        reducer = DocumentKnowledgeReducer(
            ingest_profile=make_profile(max_input_tokens=5_500),
            call_llm=caller,
        )

        with self.assertRaises(DocumentReduceError) as context:
            reducer.reduce(
                extractions,
                document_filename="manual.md",
                today=date(2026, 10, 19),
                job_id=24,
            )

        self.assertEqual(
            context.exception.category,
            "section_chunk_coverage_invalid",
        )
        self.assertEqual(len(caller.calls), 1)

    def test_stage_output_budgets_are_capped_by_profile(self) -> None:
        extractions = (make_extraction(1),)
        caller = FakeReduceLLMCaller([plan_payload(extractions)])
        reducer = DocumentKnowledgeReducer(
            ingest_profile=make_profile(max_output_tokens=1_024),
            call_llm=caller,
        )

        reducer.reduce(
            extractions,
            document_filename="manual.md",
            today=date(2026, 10, 19),
            job_id=25,
        )

        self.assertEqual(caller.calls[0][1], 1_024)

    def test_reduce_output_budget_accepts_smaller_configured_value(self) -> None:
        extractions = (make_extraction(1),)
        caller = FakeReduceLLMCaller([plan_payload(extractions)])
        reducer = DocumentKnowledgeReducer(
            ingest_profile=make_profile(),
            call_llm=caller,
            reduce_max_output_tokens=3_072,
            section_max_output_tokens=2_048,
        )

        reducer.reduce(
            extractions,
            document_filename="manual.md",
            today=date(2026, 10, 19),
            job_id=26,
        )

        self.assertEqual(caller.calls[0][1], 3_072)


if __name__ == "__main__":
    unittest.main()
