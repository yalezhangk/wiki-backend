from __future__ import annotations

import json
import unittest
from datetime import date

from app.llm_config import (
    IngestModelCapabilities,
    IngestModelProfile,
    LLMProfile,
    LLMResponseTruncatedError,
)
from app.schemas.ingest_large_document import (
    IngestChunkExtraction,
    IngestGeneratedKnowledgeCreate,
    IngestGeneratedKnowledgePatch,
    IngestGeneratedKnowledgeSkip,
    IngestGeneratedSourcePage,
    IngestKnowledgePlan,
)
from app.services.ingest_context import WikiCandidate
from app.services.ingest_large_document_status import LargeDocumentProgressEvent
from app.services.ingest_page_generation import (
    LargeDocumentPageGenerator,
    PageGenerationError,
)
from app.services.ingest_plan_wiki_retrieval import KnowledgePlanWikiContext
from app.services.ingest_result_assembly import (
    IngestResultAssemblyError,
    LargeDocumentResultAssembler,
)


SNAPSHOT_HASH = "a" * 64


class FakePageLLMCaller:
    def __init__(
        self,
        responses: list[str | Exception],
    ) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, prompt: str, *, max_tokens: int) -> str:
        self.calls.append((prompt, max_tokens))
        if not self._responses:
            raise AssertionError("unexpected page-generation LLM call")
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


def make_extractions() -> tuple[IngestChunkExtraction, ...]:
    values: list[IngestChunkExtraction] = []
    for number, statement in (
        (1, "RM6 的额定电压为 24kV。"),
        (2, "绝缘系统要求环境温度不高于 40°C。"),
    ):
        chunk_id = f"chunk-{number:04d}-hash"
        values.append(
            IngestChunkExtraction.model_validate(
                {
                    "extraction_status": "succeeded",
                    "extraction_error": None,
                    "chunk_id": chunk_id,
                    "section_title": f"章节 {number}",
                    "summary": statement,
                    "evidence": [
                        {
                            "evidence_id": f"{chunk_id}:e001",
                            "statement": statement,
                            "evidence_text": statement,
                            "source_lines": str(number),
                            "category": "specification",
                        }
                    ],
                    "entities": ["RM6"],
                    "concepts": ["绝缘系统"],
                    "internal_conflicts": [],
                }
            )
        )
    return tuple(values)


def make_plan(
    extractions: tuple[IngestChunkExtraction, ...],
) -> IngestKnowledgePlan:
    first_id = extractions[0].evidence[0].evidence_id
    second_id = extractions[1].evidence[0].evidence_id
    return IngestKnowledgePlan.model_validate(
        {
            "planning_status": "succeeded",
            "planning_error": None,
            "title": "RM6 技术手册",
            "slug": "rm6-technical-manual",
            "document_summary": "RM6 额定参数和绝缘要求。",
            "key_claims": [
                "额定电压为 24kV。",
                "环境温度不高于 40°C。",
            ],
            "covered_chunk_ids": [item.chunk_id for item in extractions],
            "planned_pages": [
                {
                    "page_type": "source",
                    "title": "RM6 技术手册",
                    "path": "sources/rm6-technical-manual.md",
                    "purpose": "保存完整来源",
                    "supporting_chunk_ids": [item.chunk_id for item in extractions],
                    "supporting_evidence_ids": [first_id, second_id],
                },
                {
                    "page_type": "entity",
                    "title": "RM6",
                    "path": "entities/rm6.md",
                    "purpose": "记录 RM6 额定参数",
                    "supporting_chunk_ids": [extractions[0].chunk_id],
                    "supporting_evidence_ids": [first_id],
                },
                {
                    "page_type": "concept",
                    "title": "绝缘系统",
                    "path": "concepts/insulation-system.md",
                    "purpose": "记录 Insulation 环境要求",
                    "supporting_chunk_ids": [extractions[1].chunk_id],
                    "supporting_evidence_ids": [second_id],
                },
            ],
            "patch_intents": [],
            "contradictions": ["来源内部没有待消解冲突。"],
        }
    )


def make_wiki_context() -> KnowledgePlanWikiContext:
    candidate = WikiCandidate(
        path="wiki/concepts/insulation.md",
        heading="Requirements",
        page_type="concepts",
        content="## Requirements\nInsulation requirements from older sources.",
        snapshot_hash=SNAPSHOT_HASH,
        score=12,
    )
    return KnowledgePlanWikiContext(
        rendered_context=candidate.rendered,
        selected=(candidate,),
        skipped=(),
        selected_page_hashes={"concepts/insulation.md": SNAPSHOT_HASH},
    )


def source_response(*, content: str | None = None) -> str:
    source_content = content or (
        "---\n"
        'title: "RM6 技术手册"\n'
        "type: source\n"
        "tags: [RM6]\n"
        "date: 2026-10-19\n"
        "---\n\n"
        "# RM6 技术手册\n\n"
        "额定电压为 24kV，参见 [[RM6]]。"
        "绝缘要求参见 [[insulation]]。\n"
    )
    return json.dumps(
        {
            "generation_status": "succeeded",
            "generation_error": None,
            "target_id": "source",
            "title": "RM6 技术手册",
            "slug": "rm6-technical-manual",
            "content": source_content,
        },
        ensure_ascii=False,
    )


def create_response() -> str:
    return json.dumps(
        {
            "generation_status": "succeeded",
            "generation_error": None,
            "target_id": "entities/rm6.md",
            "action": "create",
            "page_type": "entity",
            "title": "RM6",
            "path": "entities/rm6.md",
            "content": "# RM6\n\n额定电压为 24kV。\n",
            "index_summary": "RM6 的主要额定参数。",
        },
        ensure_ascii=False,
    )


def patch_response(*, base_hash: str = SNAPSHOT_HASH) -> str:
    return json.dumps(
        {
            "generation_status": "succeeded",
            "generation_error": None,
            "target_id": "concepts/insulation-system.md",
            "action": "patch",
            "page_type": "concept",
            "path": "concepts/insulation.md",
            "base_hash": base_hash,
            "operation": "replace_section",
            "heading": "Requirements",
            "content": "环境温度不得高于 40°C。",
        },
        ensure_ascii=False,
    )


class LargeDocumentResultAssemblerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.extractions = make_extractions()
        self.plan = make_plan(self.extractions)
        self.source = IngestGeneratedSourcePage.model_validate_json(
            source_response()
        )
        self.created = IngestGeneratedKnowledgeCreate.model_validate_json(
            create_response()
        )
        self.patch = IngestGeneratedKnowledgePatch.model_validate_json(
            patch_response()
        )
        self.assembler = LargeDocumentResultAssembler()

    def test_assembles_existing_contract_in_plan_order(self) -> None:
        result = self.assembler.assemble(
            plan=self.plan,
            source_result=self.source,
            knowledge_results=[self.patch, self.created],
            selected_page_hashes={"concepts/insulation.md": SNAPSHOT_HASH},
            today=date(2026, 10, 19),
        )

        self.assertEqual(result.data.title, self.plan.title)
        self.assertEqual(
            [page.path for page in result.data.entity_pages],
            ["entities/rm6.md"],
        )
        self.assertEqual(
            [patch.path for patch in result.data.concept_patches],
            ["concepts/insulation.md"],
        )
        self.assertEqual(
            result.data.entity_index_entries[0].entry,
            "- [RM6](entities/rm6.md) - RM6 的主要额定参数。",
        )
        self.assertEqual(result.data.contradictions, self.plan.contradictions)
        self.assertIn("[2026-10-19] ingest", result.data.log_entry)
        self.assertEqual(
            result.selected_page_hashes,
            {"concepts/insulation.md": SNAPSHOT_HASH},
        )

    def test_missing_planned_result_is_rejected(self) -> None:
        with self.assertRaises(IngestResultAssemblyError) as context:
            self.assembler.assemble(
                plan=self.plan,
                source_result=self.source,
                knowledge_results=[self.created],
                selected_page_hashes={"concepts/insulation.md": SNAPSHOT_HASH},
                today=date(2026, 10, 19),
            )

        self.assertEqual(
            context.exception.category,
            "assembly_plan_coverage_invalid",
        )

    def test_duplicate_target_result_is_rejected(self) -> None:
        with self.assertRaises(IngestResultAssemblyError) as context:
            self.assembler.assemble(
                plan=self.plan,
                source_result=self.source,
                knowledge_results=[self.created, self.created, self.patch],
                selected_page_hashes={"concepts/insulation.md": SNAPSHOT_HASH},
                today=date(2026, 10, 19),
            )

        self.assertEqual(
            context.exception.category,
            "assembly_result_target_duplicate",
        )

    def test_patch_hash_must_match_selected_snapshot(self) -> None:
        with self.assertRaises(IngestResultAssemblyError) as context:
            self.assembler.assemble(
                plan=self.plan,
                source_result=self.source,
                knowledge_results=[self.created, self.patch],
                selected_page_hashes={"concepts/insulation.md": "b" * 64},
                today=date(2026, 10, 19),
            )

        self.assertEqual(
            context.exception.category,
            "assembly_patch_hash_mismatch",
        )

    def test_multiple_results_cannot_mutate_same_output_path(self) -> None:
        plan_payload = self.plan.model_dump(mode="json")
        plan_payload["planned_pages"][1].update(
            {
                "page_type": "concept",
                "title": "RM6 额定参数",
                "path": "concepts/rm6-ratings.md",
            }
        )
        plan = IngestKnowledgePlan.model_validate(plan_payload)
        first_patch = IngestGeneratedKnowledgePatch.model_validate(
            {
                "generation_status": "succeeded",
                "generation_error": None,
                "target_id": "concepts/rm6-ratings.md",
                "action": "patch",
                "page_type": "concept",
                "path": "concepts/insulation.md",
                "base_hash": SNAPSHOT_HASH,
                "operation": "replace_section",
                "heading": "Requirements",
                "content": "补充 RM6 额定参数。",
            }
        )

        with self.assertRaises(IngestResultAssemblyError) as context:
            self.assembler.assemble(
                plan=plan,
                source_result=self.source,
                knowledge_results=[first_patch, self.patch],
                selected_page_hashes={"concepts/insulation.md": SNAPSHOT_HASH},
                today=date(2026, 10, 19),
            )

        self.assertEqual(
            context.exception.category,
            "assembly_output_path_duplicate",
        )

    def test_create_cannot_use_existing_snapshot_path(self) -> None:
        with self.assertRaises(IngestResultAssemblyError) as context:
            self.assembler.assemble(
                plan=self.plan,
                source_result=self.source,
                knowledge_results=[self.created, self.patch],
                selected_page_hashes={
                    "entities/rm6.md": "c" * 64,
                    "concepts/insulation.md": SNAPSHOT_HASH,
                },
                today=date(2026, 10, 19),
            )

        self.assertEqual(
            context.exception.category,
            "assembly_create_existing_path",
        )

    def test_skip_produces_no_page_patch_or_index_entry(self) -> None:
        skipped = IngestGeneratedKnowledgeSkip.model_validate(
            {
                "generation_status": "succeeded",
                "generation_error": None,
                "target_id": "concepts/insulation-system.md",
                "action": "skip",
                "page_type": "concept",
                "reason": "现有内容已经充分覆盖。",
            }
        )

        result = self.assembler.assemble(
            plan=self.plan,
            source_result=self.source,
            knowledge_results=[self.created, skipped],
            selected_page_hashes={"concepts/insulation.md": SNAPSHOT_HASH},
            today=date(2026, 10, 19),
        )

        self.assertEqual(result.data.concept_pages, [])
        self.assertEqual(result.data.concept_patches, [])
        self.assertEqual(result.data.concept_index_entries, [])


class LargeDocumentPageGeneratorTests(unittest.TestCase):
    def test_generates_each_page_separately_and_assembles_existing_contract(self) -> None:
        extractions = make_extractions()
        plan = make_plan(extractions)
        caller = FakePageLLMCaller(
            [create_response(), patch_response(), source_response()]
        )
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )
        progress: list[LargeDocumentProgressEvent] = []

        result = generator.generate(
            plan=plan,
            extractions=extractions,
            wiki_context=make_wiki_context(),
            today=date(2026, 10, 19),
            job_id=41,
            progress_callback=progress.append,
        )

        self.assertEqual(len(caller.calls), 3)
        self.assertEqual([item[1] for item in caller.calls], [4_096, 4_096, 8_192])
        self.assertIn(extractions[0].evidence[0].statement, caller.calls[0][0])
        self.assertNotIn(extractions[1].evidence[0].statement, caller.calls[0][0])
        self.assertIn(extractions[1].evidence[0].statement, caller.calls[1][0])
        self.assertIn("concepts/insulation.md", caller.calls[1][0])
        self.assertIn(SNAPSHOT_HASH, caller.calls[1][0])
        self.assertIn(extractions[0].evidence[0].statement, caller.calls[2][0])
        self.assertIn(extractions[1].evidence[0].statement, caller.calls[2][0])
        self.assertEqual([page.path for page in result.data.entity_pages], ["entities/rm6.md"])
        self.assertEqual(result.data.concept_pages, [])
        self.assertEqual(
            [patch.path for patch in result.data.concept_patches],
            ["concepts/insulation.md"],
        )
        self.assertEqual(
            result.data.entity_index_entries[0].entry,
            "- [RM6](entities/rm6.md) - RM6 的主要额定参数。",
        )
        self.assertEqual(
            result.data.index_entry,
            "- [RM6 技术手册](sources/rm6-technical-manual.md) - "
            "RM6 额定参数和绝缘要求。",
        )
        self.assertEqual(result.selected_page_hashes, make_wiki_context().selected_page_hashes)
        self.assertEqual(
            [
                (event.phase, event.completed, event.total, event.item_id)
                for event in progress
            ],
            [
                ("page_generation", 0, 3, None),
                ("page_generation", 1, 3, "entities/rm6.md"),
                (
                    "page_generation",
                    2,
                    3,
                    "concepts/insulation-system.md",
                ),
                ("page_generation", 3, 3, "source"),
                ("result_assembly", 1, 1, "document"),
            ],
        )

    def test_page_output_budgets_accept_distinct_configured_values(self) -> None:
        extractions = make_extractions()
        caller = FakePageLLMCaller(
            [create_response(), patch_response(), source_response()]
        )
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
            source_max_output_tokens=6_144,
            knowledge_max_output_tokens=3_072,
        )

        generator.generate(
            plan=make_plan(extractions),
            extractions=extractions,
            wiki_context=make_wiki_context(),
            today=date(2026, 10, 19),
            job_id=51,
        )

        self.assertEqual(
            [max_tokens for _, max_tokens in caller.calls],
            [3_072, 3_072, 6_144],
        )

    def test_source_prompt_uses_only_source_plan_evidence(self) -> None:
        extractions = make_extractions()
        payload = make_plan(extractions).model_dump(mode="json")
        payload["planned_pages"][0]["supporting_chunk_ids"] = [
            extractions[0].chunk_id
        ]
        payload["planned_pages"][0]["supporting_evidence_ids"] = [
            extractions[0].evidence[0].evidence_id
        ]
        caller = FakePageLLMCaller(
            [create_response(), patch_response(), source_response()]
        )
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        generator.generate(
            plan=IngestKnowledgePlan.model_validate(payload),
            extractions=extractions,
            wiki_context=make_wiki_context(),
            today=date(2026, 10, 19),
            job_id=53,
        )

        source_prompt = caller.calls[2][0]
        self.assertIn(extractions[0].evidence[0].statement, source_prompt)
        self.assertNotIn(extractions[1].evidence[0].statement, source_prompt)

    def test_page_output_budgets_are_capped_by_model_profile(self) -> None:
        extractions = make_extractions()
        caller = FakePageLLMCaller(
            [create_response(), patch_response(), source_response()]
        )
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(max_output_tokens=1_024),
            call_llm=caller,
        )

        generator.generate(
            plan=make_plan(extractions),
            extractions=extractions,
            wiki_context=make_wiki_context(),
            today=date(2026, 10, 19),
            job_id=52,
        )

        self.assertEqual(
            [max_tokens for _, max_tokens in caller.calls],
            [1_024, 1_024, 1_024],
        )

    def test_unknown_page_evidence_fails_before_any_model_call(self) -> None:
        extractions = make_extractions()
        payload = make_plan(extractions).model_dump(mode="json")
        payload["planned_pages"][1]["supporting_evidence_ids"] = ["unknown:e001"]
        plan = IngestKnowledgePlan.model_validate(payload)
        caller = FakePageLLMCaller([source_response()])
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(PageGenerationError) as context:
            generator.generate(
                plan=plan,
                extractions=extractions,
                wiki_context=make_wiki_context(),
                today=date(2026, 10, 19),
                job_id=42,
            )

        self.assertEqual(context.exception.category, "page_evidence_unknown")
        self.assertEqual(caller.calls, [])

    def test_unknown_source_evidence_fails_before_any_model_call(self) -> None:
        extractions = make_extractions()
        payload = make_plan(extractions).model_dump(mode="json")
        payload["planned_pages"][0]["supporting_evidence_ids"] = [
            "unknown:e001"
        ]
        plan = IngestKnowledgePlan.model_validate(payload)
        caller = FakePageLLMCaller([source_response()])
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(PageGenerationError) as context:
            generator.generate(
                plan=plan,
                extractions=extractions,
                wiki_context=make_wiki_context(),
                today=date(2026, 10, 19),
                job_id=54,
            )

        self.assertEqual(context.exception.category, "page_evidence_unknown")
        self.assertEqual(caller.calls, [])

    def test_entity_patch_uses_entities_wiki_candidates(self) -> None:
        extractions = make_extractions()
        entity_hash = "d" * 64
        candidate = WikiCandidate(
            path="wiki/entities/rm6-existing.md",
            heading="Ratings",
            page_type="entities",
            content="## Ratings\nRM6 existing ratings.",
            snapshot_hash=entity_hash,
            score=10,
        )
        wiki_context = KnowledgePlanWikiContext(
            rendered_context=candidate.rendered,
            selected=(candidate,),
            skipped=(),
            selected_page_hashes={"entities/rm6-existing.md": entity_hash},
        )
        entity_patch = json.dumps(
            {
                "generation_status": "succeeded",
                "generation_error": None,
                "target_id": "entities/rm6.md",
                "action": "patch",
                "page_type": "entity",
                "path": "entities/rm6-existing.md",
                "base_hash": entity_hash,
                "operation": "replace_section",
                "heading": "Ratings",
                "content": "额定电压为 24kV。",
            },
            ensure_ascii=False,
        )
        concept_skip = json.dumps(
            {
                "generation_status": "succeeded",
                "generation_error": None,
                "target_id": "concepts/insulation-system.md",
                "action": "skip",
                "page_type": "concept",
                "reason": "未检索到需要修改的相关页面。",
            },
            ensure_ascii=False,
        )
        source = source_response(
            content=(
                "---\n"
                'title: "RM6 技术手册"\n'
                "type: source\n"
                "tags: [RM6]\n"
                "date: 2026-10-19\n"
                "---\n\n"
                "# RM6 技术手册\n\n"
                "[[rm6-existing]]\n"
            )
        )
        caller = FakePageLLMCaller([entity_patch, concept_skip, source])
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        result = generator.generate(
            plan=make_plan(extractions),
            extractions=extractions,
            wiki_context=wiki_context,
            today=date(2026, 10, 19),
            job_id=50,
        )

        self.assertIn("entities/rm6-existing.md", caller.calls[0][0])
        self.assertEqual(
            [patch.path for patch in result.data.entity_patches],
            ["entities/rm6-existing.md"],
        )

    def test_invalid_source_frontmatter_rejects_final_assembly(self) -> None:
        extractions = make_extractions()
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=FakePageLLMCaller([]),
        )

        with self.assertRaises(PageGenerationError) as context:
            generator._validate_source_result(
                IngestGeneratedSourcePage.model_validate_json(
                    source_response(content="# Missing frontmatter\n")
                ),
                plan=make_plan(extractions),
                today=date(2026, 10, 19),
                allowed_link_targets=set(),
            )

        self.assertEqual(context.exception.category, "source_frontmatter_invalid")

    def test_source_frontmatter_rejects_list_items_after_scalar_key(self) -> None:
        extractions = make_extractions()
        malformed_content = (
            "---\n"
            'title: "RM6 技术手册"\n'
            "type: source\n"
            "tags:\n"
            "date: 2026-10-19\n"
            "  - RM6\n"
            "---\n\n# RM6\n"
        )
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=FakePageLLMCaller([]),
        )

        with self.assertRaises(PageGenerationError) as context:
            generator._validate_source_result(
                IngestGeneratedSourcePage.model_validate_json(
                    source_response(content=malformed_content)
                ),
                plan=make_plan(extractions),
                today=date(2026, 10, 19),
                allowed_link_targets=set(),
            )

        self.assertEqual(context.exception.category, "source_frontmatter_invalid")

    def test_source_prompt_uses_path_target_with_display_alias(self) -> None:
        extractions = make_extractions()
        caller = FakePageLLMCaller(
            [create_response(), patch_response(), source_response()]
        )
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        generator.generate(
            plan=make_plan(extractions),
            extractions=extractions,
            wiki_context=make_wiki_context(),
            today=date(2026, 10, 19),
            job_id=43,
        )

        self.assertIn("[[entities/rm6|RM6]]", caller.calls[-1][0])

    def test_source_page_rejects_unplanned_wikilink(self) -> None:
        extractions = make_extractions()
        caller = FakePageLLMCaller(
            [
                create_response(),
                patch_response(),
                source_response(content=(
                    "---\n"
                    'title: "RM6 技术手册"\n'
                    "type: source\n"
                    "tags: [RM6]\n"
                    "date: 2026-10-19\n"
                    "---\n\n# RM6 技术手册\n\n[[Invented Page]]\n"
                )),
            ]
        )
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(PageGenerationError) as context:
            generator.generate(
                plan=make_plan(extractions),
                extractions=extractions,
                wiki_context=make_wiki_context(),
                today=date(2026, 10, 19),
                job_id=44,
            )

        self.assertEqual(context.exception.category, "page_wikilink_unknown")

    def test_patch_requires_exact_retrieved_hash(self) -> None:
        extractions = make_extractions()
        caller = FakePageLLMCaller(
            [create_response(), patch_response(base_hash="b" * 64)]
        )
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(PageGenerationError) as context:
            generator.generate(
                plan=make_plan(extractions),
                extractions=extractions,
                wiki_context=make_wiki_context(),
                today=date(2026, 10, 19),
                job_id=45,
            )

        self.assertEqual(context.exception.category, "knowledge_patch_hash_mismatch")
        self.assertEqual(len(caller.calls), 2)

    def test_skip_does_not_create_page_or_patch(self) -> None:
        extractions = make_extractions()
        skip = json.dumps(
            {
                "generation_status": "succeeded",
                "generation_error": None,
                "target_id": "concepts/insulation-system.md",
                "action": "skip",
                "page_type": "concept",
                "reason": "既有页面已经包含相同要求。",
            },
            ensure_ascii=False,
        )
        caller = FakePageLLMCaller([create_response(), skip, source_response()])
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        result = generator.generate(
            plan=make_plan(extractions),
            extractions=extractions,
            wiki_context=make_wiki_context(),
            today=date(2026, 10, 19),
            job_id=46,
        )

        self.assertEqual(result.data.concept_pages, [])
        self.assertEqual(result.data.concept_patches, [])

    def test_invalid_json_is_repaired_once_without_evidence_or_wiki_context(self) -> None:
        extractions = make_extractions()
        caller = FakePageLLMCaller(
            [create_response(), patch_response(), "not json", source_response()]
        )
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        generator.generate(
            plan=make_plan(extractions),
            extractions=extractions,
            wiki_context=make_wiki_context(),
            today=date(2026, 10, 19),
            job_id=47,
        )

        self.assertEqual(len(caller.calls), 4)
        self.assertIn("not json", caller.calls[3][0])
        self.assertNotIn(extractions[0].evidence[0].statement, caller.calls[3][0])
        self.assertNotIn(SNAPSHOT_HASH, caller.calls[3][0])

    def test_model_failure_stops_later_page_calls(self) -> None:
        extractions = make_extractions()
        failure = json.dumps(
            {
                "generation_status": "failed",
                "generation_error": "无法可靠生成实体页",
                "target_id": "entities/rm6.md",
            },
            ensure_ascii=False,
        )
        caller = FakePageLLMCaller(
            [failure, patch_response(), source_response()]
        )
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        with self.assertRaises(PageGenerationError) as context:
            generator.generate(
                plan=make_plan(extractions),
                extractions=extractions,
                wiki_context=make_wiki_context(),
                today=date(2026, 10, 19),
                job_id=48,
            )

        self.assertEqual(context.exception.category, "page_generation_failed")
        self.assertEqual(len(caller.calls), 1)

    def test_truncated_response_retries_only_current_page(self) -> None:
        extractions = make_extractions()
        truncated = LLMResponseTruncatedError(
            model="ollama_chat/qwen3.8:27b",
            max_tokens=4_096,
            finish_reason="length",
            response_content='{"generation_status":"succeeded"',
        )
        caller = FakePageLLMCaller(
            [
                truncated,
                create_response(),
                patch_response(),
                source_response(),
            ]
        )
        generator = LargeDocumentPageGenerator(
            ingest_profile=make_profile(),
            call_llm=caller,
        )

        generator.generate(
            plan=make_plan(extractions),
            extractions=extractions,
            wiki_context=make_wiki_context(),
            today=date(2026, 10, 19),
            job_id=49,
        )

        self.assertEqual(len(caller.calls), 4)
        self.assertEqual(caller.calls[0][0], caller.calls[1][0])
        source_calls = sum(
            "Generate the single Source page" in call[0]
            for call in caller.calls
        )
        self.assertEqual(source_calls, 1)


if __name__ == "__main__":
    unittest.main()
