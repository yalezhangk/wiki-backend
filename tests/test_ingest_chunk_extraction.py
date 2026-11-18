from __future__ import annotations

import json
import unittest
from datetime import date

from app.llm_config import IngestModelCapabilities, IngestModelProfile, LLMProfile
from app.services.ingest_chunk_extraction import (
    ChunkEvidenceExtractionError,
    ChunkEvidenceExtractor,
)
from app.services.ingest_large_document import MarkdownChunk
from app.services.ingest_large_document_status import LargeDocumentProgressEvent


class FakeChunkLLMCaller:
    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, prompt: str, *, max_tokens: int) -> str:
        self.calls.append((prompt, max_tokens))
        if not self._responses:
            raise AssertionError("unexpected chunk LLM call")
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


def make_chunk(
    chunk_id: str,
    *,
    start_line: int,
    primary_text: str,
    overlap_text: str = "",
    structural_context: str = "",
) -> MarkdownChunk:
    line_count = max(1, len(primary_text.splitlines()))
    return MarkdownChunk(
        chunk_id=chunk_id,
        start_line=start_line,
        end_line=start_line + line_count - 1,
        breadcrumbs=("产品", "技术规格"),
        primary_text=primary_text,
        overlap_text=overlap_text,
        structural_context=structural_context,
        estimated_tokens=len(primary_text),
    )


def successful_payload(
    chunk: MarkdownChunk,
    *,
    evidence: list[dict[str, str]] | None = None,
) -> str:
    return json.dumps(
        {
            "extraction_status": "succeeded",
            "extraction_error": None,
            "chunk_id": chunk.chunk_id,
            "section_title": "技术规格",
            "summary": "该章节给出产品技术规格。",
            "evidence": evidence or [],
            "entities": ["RM6 SeT", "rm6 set"],
            "concepts": ["中压开关柜"],
            "internal_conflicts": [],
        },
        ensure_ascii=False,
    )


class ChunkEvidenceExtractorTests(unittest.TestCase):
    def test_extracts_chunks_sequentially_and_reports_progress(self) -> None:
        first = make_chunk(
            "chunk-0001-aaaa",
            start_line=10,
            primary_text="额定电压为24kV。\n主回路寿命40年以上。\n",
        )
        second = make_chunk(
            "chunk-0002-bbbb",
            start_line=20,
            primary_text="气箱采用3mm 304不锈钢。\n",
            overlap_text="上一节结尾。",
        )
        caller = FakeChunkLLMCaller(
            [
                successful_payload(
                    first,
                    evidence=[
                        {
                            "evidence_id": f"{first.chunk_id}:e001",
                            "statement": "产品额定电压为24kV。",
                            "evidence_text": "额定电压为24kV",
                            "source_lines": "10",
                            "category": "specification",
                        }
                    ],
                ),
                successful_payload(
                    second,
                    evidence=[
                        {
                            "evidence_id": f"{second.chunk_id}:e001",
                            "statement": "气箱采用3mm 304不锈钢。",
                            "evidence_text": "3mm 304不锈钢",
                            "source_lines": "20",
                            "category": "specification",
                        }
                    ],
                ),
            ]
        )
        progress: list[LargeDocumentProgressEvent] = []
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        results = extractor.extract(
            (first, second),
            today=date(2026, 10, 19),
            progress_callback=progress.append,
        )

        self.assertEqual([result.chunk_id for result in results], [first.chunk_id, second.chunk_id])
        self.assertEqual(len(caller.calls), 2)
        self.assertEqual([call[1] for call in caller.calls], [4_096, 4_096])
        self.assertEqual(
            [
                (event.completed, event.total, event.item_id)
                for event in progress
            ],
            [
                (0, 2, None),
                (1, 2, first.chunk_id),
                (2, 2, second.chunk_id),
            ],
        )
        self.assertEqual(
            [event.phase for event in progress],
            [
                "chunk_extraction",
                "chunk_extraction",
                "chunk_extraction",
            ],
        )
        self.assertIn("L10: 额定电压为24kV。", caller.calls[0][0])
        self.assertIn("L11: 主回路寿命40年以上。", caller.calls[0][0])
        self.assertIn("at most 20 highest-value evidence items", caller.calls[0][0])
        self.assertIn("产品 / 技术规格", caller.calls[0][0])
        self.assertNotIn("Current Wiki state", caller.calls[0][0])
        self.assertEqual(results[0].entities, ["RM6 SeT"])

    def test_low_information_chunk_may_return_empty_evidence(self) -> None:
        chunk = make_chunk(
            "chunk-0001-empty",
            start_line=1,
            primary_text="公司宣传页眉。\n",
        )
        caller = FakeChunkLLMCaller([successful_payload(chunk)])
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        result = extractor.extract((chunk,), today=date(2026, 10, 19))[0]

        self.assertEqual(result.evidence, [])

    def test_invalid_json_is_repaired_once_without_resending_source(self) -> None:
        chunk = make_chunk(
            "chunk-0001-repair",
            start_line=1,
            primary_text="仅存在于原始来源的24kV规格。\n",
        )
        caller = FakeChunkLLMCaller(["not json", successful_payload(chunk)])
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        result = extractor.extract((chunk,), today=date(2026, 10, 19))[0]

        self.assertEqual(result.chunk_id, chunk.chunk_id)
        self.assertEqual(len(caller.calls), 2)
        self.assertNotIn(chunk.primary_text, caller.calls[1][0])
        self.assertNotIn("PRIMARY SOURCE", caller.calls[1][0])
        self.assertIn("not json", caller.calls[1][0])
        self.assertIn("at most 20 highest-value evidence items", caller.calls[1][0])

    def test_overlong_evidence_list_is_repaired_to_stage_limit(self) -> None:
        chunk = make_chunk(
            "chunk-0001-compact",
            start_line=1,
            primary_text="额定电压为24kV。\n",
        )
        excessive_evidence = [
            {
                "evidence_id": f"{chunk.chunk_id}:e{number:03d}",
                "statement": "额定电压为24kV。",
                "evidence_text": "额定电压为24kV",
                "source_lines": "1",
                "category": "specification",
            }
            for number in range(1, 22)
        ]
        caller = FakeChunkLLMCaller(
            [
                successful_payload(chunk, evidence=excessive_evidence),
                successful_payload(chunk, evidence=excessive_evidence[:20]),
            ]
        )
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        result = extractor.extract((chunk,), today=date(2026, 10, 19))[0]

        self.assertEqual(len(result.evidence), 20)
        self.assertEqual(len(caller.calls), 2)
        self.assertIn("chunk_schema_invalid", caller.calls[1][0])
        self.assertIn("at most 20 highest-value evidence items", caller.calls[1][0])

    def test_invalid_json_after_repair_fails(self) -> None:
        chunk = make_chunk("chunk-0001-invalid", start_line=1, primary_text="正文。\n")
        caller = FakeChunkLLMCaller(["not json", "still not json"])
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(ChunkEvidenceExtractionError) as context:
            extractor.extract((chunk,), today=date(2026, 10, 19))

        self.assertEqual(context.exception.category, "chunk_json_invalid")
        self.assertEqual(len(caller.calls), 2)

    def test_valid_json_with_invalid_schema_is_repaired_against_source(self) -> None:
        chunk = make_chunk("chunk-0001-schema", start_line=1, primary_text="正文。\n")
        caller = FakeChunkLLMCaller(
            [
                json.dumps({"chunk_id": chunk.chunk_id}),
                successful_payload(chunk),
            ]
        )
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        result = extractor.extract((chunk,), today=date(2026, 10, 19))[0]

        self.assertEqual(result.chunk_id, chunk.chunk_id)
        self.assertEqual(len(caller.calls), 2)
        self.assertIn("chunk_schema_invalid", caller.calls[1][0])

    def test_grounding_repair_accepts_long_formula_excerpt(self) -> None:
        formula = "X_{A}=0.1826+0.115+0.1826*0.115/0.3122;" * 6
        chunk = make_chunk(
            "chunk-0001-formula",
            start_line=1,
            primary_text=f"{formula}\n",
        )
        repaired = successful_payload(
            chunk,
            evidence=[
                {
                    "evidence_id": f"{chunk.chunk_id}:e001",
                    "statement": "公式给出 X_A 的计算过程。",
                    "evidence_text": formula,
                    "source_lines": "1",
                    "category": "specification",
                }
            ],
        )
        caller = FakeChunkLLMCaller([json.dumps({"chunk_id": chunk.chunk_id}), repaired])
        extractor = ChunkEvidenceExtractor(ingest_profile=make_profile(), call_llm=caller)

        result = extractor.extract((chunk,), today=date(2026, 10, 19))[0]

        self.assertEqual(result.evidence[0].evidence_text, formula)

    def test_repair_limits_auxiliary_lists_to_contract_size(self) -> None:
        chunk = make_chunk("chunk-0001-list", start_line=1, primary_text="正文。\n")
        oversized = json.loads(successful_payload(chunk))
        oversized["entities"] = [f"实体{number}" for number in range(21)]
        caller = FakeChunkLLMCaller(
            [json.dumps(oversized, ensure_ascii=False)] * 2
        )
        extractor = ChunkEvidenceExtractor(ingest_profile=make_profile(), call_llm=caller)

        result = extractor.extract((chunk,), today=date(2026, 10, 19))[0]

        self.assertEqual(len(result.entities), 20)

    def test_formula_backslashes_in_model_json_are_recovered(self) -> None:
        formula = r"I_{K2}^{\prime\prime} = \frac{1}{2}"
        chunk = make_chunk(
            "chunk-0001-tex",
            start_line=1,
            primary_text=f"{formula}\n",
        )
        payload = successful_payload(
            chunk,
            evidence=[
                {
                    "evidence_id": f"{chunk.chunk_id}:e001",
                    "statement": "K2 点电流公式",
                    "evidence_text": formula,
                    "source_lines": "1",
                    "category": "specification",
                }
            ],
        )
        invalid_json = payload.replace(r"\\prime", r"\prime").replace(
            r"\\frac", r"\frac"
        )
        caller = FakeChunkLLMCaller([invalid_json])
        extractor = ChunkEvidenceExtractor(ingest_profile=make_profile(), call_llm=caller)

        result = extractor.extract((chunk,), today=date(2026, 10, 19))[0]

        self.assertEqual(result.evidence[0].evidence_text, formula)
        self.assertEqual(len(caller.calls), 1)

    def test_model_failure_stops_before_later_chunks(self) -> None:
        first = make_chunk("chunk-0001-failed", start_line=1, primary_text="正文一。\n")
        second = make_chunk("chunk-0002-unused", start_line=2, primary_text="正文二。\n")
        caller = FakeChunkLLMCaller(
            [
                json.dumps(
                    {
                        "extraction_status": "failed",
                        "extraction_error": "无法可靠处理",
                        "chunk_id": first.chunk_id,
                    },
                    ensure_ascii=False,
                ),
                successful_payload(second),
            ]
        )
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(ChunkEvidenceExtractionError) as context:
            extractor.extract((first, second), today=date(2026, 10, 19))

        self.assertEqual(context.exception.category, "chunk_extraction_failed")
        self.assertEqual(len(caller.calls), 1)

    def test_wrong_chunk_identity_is_rejected(self) -> None:
        chunk = make_chunk("chunk-0001-right", start_line=1, primary_text="正文。\n")
        wrong = make_chunk("chunk-9999-wrong", start_line=1, primary_text="正文。\n")
        caller = FakeChunkLLMCaller([successful_payload(wrong)])
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(ChunkEvidenceExtractionError) as context:
            extractor.extract((chunk,), today=date(2026, 10, 19))

        self.assertEqual(context.exception.category, "chunk_identity_mismatch")

    def test_evidence_line_range_and_verbatim_grounding_are_required(self) -> None:
        chunk = make_chunk(
            "chunk-0001-grounding",
            start_line=10,
            primary_text="额定电压为24kV。\n使用寿命40年以上。\n",
        )
        invalid_line_payload = successful_payload(
            chunk,
            evidence=[
                {
                    "evidence_id": f"{chunk.chunk_id}:e001",
                    "statement": "额定电压为24kV。",
                    "evidence_text": "额定电压为24kV",
                    "source_lines": "9",
                    "category": "specification",
                }
            ],
        )
        caller = FakeChunkLLMCaller([invalid_line_payload, invalid_line_payload])
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(ChunkEvidenceExtractionError) as context:
            extractor.extract((chunk,), today=date(2026, 10, 19))

        self.assertEqual(context.exception.category, "chunk_evidence_repair_empty")

        ungrounded_payload = successful_payload(
            chunk,
            evidence=[
                {
                    "evidence_id": f"{chunk.chunk_id}:e001",
                    "statement": "额定电压为36kV。",
                    "evidence_text": "额定电压为36kV",
                    "source_lines": "10",
                    "category": "specification",
                }
            ],
        )
        second_caller = FakeChunkLLMCaller([ungrounded_payload, ungrounded_payload])
        second_extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=second_caller,
        )

        with self.assertRaises(ChunkEvidenceExtractionError) as second_context:
            second_extractor.extract((chunk,), today=date(2026, 10, 19))

        self.assertEqual(second_context.exception.category, "chunk_evidence_repair_empty")

    def test_invalid_grounding_is_repaired_once_against_numbered_source(self) -> None:
        chunk = make_chunk(
            "chunk-0001-grounding-repair",
            start_line=10,
            primary_text="额定电压为24kV。\n使用寿命40年以上。\n",
        )
        invalid = successful_payload(
            chunk,
            evidence=[
                {
                    "evidence_id": f"{chunk.chunk_id}:e001",
                    "statement": "额定电压为24kV。",
                    "evidence_text": "额定电压为24kV",
                    "source_lines": "11",
                    "category": "specification",
                }
            ],
        )
        repaired = successful_payload(
            chunk,
            evidence=[
                {
                    "evidence_id": f"{chunk.chunk_id}:e001",
                    "statement": "额定电压为24kV。",
                    "evidence_text": "额定电压为24kV",
                    "source_lines": "10",
                    "category": "specification",
                },
                {
                    "evidence_id": f"{chunk.chunk_id}:e002",
                    "statement": "额定电压为36kV。",
                    "evidence_text": "额定电压为36kV",
                    "source_lines": "10",
                    "category": "specification",
                },
            ],
        )
        caller = FakeChunkLLMCaller([invalid, repaired])
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        result = extractor.extract((chunk,), today=date(2026, 10, 19))[0]

        self.assertEqual(result.evidence[0].source_lines, "10")
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(len(caller.calls), 2)
        self.assertIn("L10: 额定电压为24kV。", caller.calls[1][0])
        self.assertIn("chunk_evidence_not_grounded", caller.calls[1][0])

    def test_markdown_table_boundaries_are_ignored_for_grounding(self) -> None:
        chunk = make_chunk(
            "chunk-0001-table",
            start_line=30,
            primary_text="| 额定电压 | 24kV |\n",
        )
        caller = FakeChunkLLMCaller(
            [
                successful_payload(
                    chunk,
                    evidence=[
                        {
                            "evidence_id": f"{chunk.chunk_id}:e001",
                            "statement": "额定电压为24kV。",
                            "evidence_text": "额定电压 24kV",
                            "source_lines": "30",
                            "category": "specification",
                        }
                    ],
                )
            ]
        )
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        result = extractor.extract((chunk,), today=date(2026, 10, 19))[0]

        self.assertEqual(len(result.evidence), 1)

    def test_duplicate_evidence_is_removed_after_one_repair(self) -> None:
        chunk = make_chunk(
            "chunk-0001-ids",
            start_line=1,
            primary_text="额定电压为24kV。\n",
        )
        evidence = {
            "evidence_id": f"{chunk.chunk_id}:e001",
            "statement": "额定电压为24kV。",
            "evidence_text": "额定电压为24kV",
            "source_lines": "1",
            "category": "specification",
        }
        duplicate_payload = successful_payload(
            chunk,
            evidence=[evidence, evidence],
        )
        caller = FakeChunkLLMCaller([duplicate_payload, duplicate_payload])
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        result = extractor.extract((chunk,), today=date(2026, 10, 19))[0]

        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(len(caller.calls), 2)

    def test_prompt_budget_is_checked_before_model_call(self) -> None:
        chunk = make_chunk(
            "chunk-0001-budget",
            start_line=1,
            primary_text="正文。\n",
        )
        caller = FakeChunkLLMCaller([successful_payload(chunk)])
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(max_input_tokens=10),
            call_llm=caller,
        )

        with self.assertRaises(ChunkEvidenceExtractionError) as context:
            extractor.extract((chunk,), today=date(2026, 10, 19))

        self.assertEqual(context.exception.category, "chunk_prompt_too_large")
        self.assertEqual(caller.calls, [])

    def test_stage_output_budget_is_capped_by_model_profile(self) -> None:
        chunk = make_chunk("chunk-0001-output", start_line=1, primary_text="正文。\n")
        caller = FakeChunkLLMCaller([successful_payload(chunk)])
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(max_output_tokens=1_024),
            call_llm=caller,
        )

        extractor.extract((chunk,), today=date(2026, 10, 19))

        self.assertEqual(caller.calls[0][1], 1_024)

    def test_stage_output_budget_accepts_smaller_configured_value(self) -> None:
        chunk = make_chunk("chunk-0001-custom", start_line=1, primary_text="正文。\n")
        caller = FakeChunkLLMCaller([successful_payload(chunk)])
        extractor = ChunkEvidenceExtractor(
            ingest_profile=make_profile(),
            call_llm=caller,
            max_output_tokens=2_048,
        )

        extractor.extract((chunk,), today=date(2026, 10, 19))

        self.assertEqual(caller.calls[0][1], 2_048)


if __name__ == "__main__":
    unittest.main()
