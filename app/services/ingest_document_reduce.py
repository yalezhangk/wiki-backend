"""将全部 chunk extraction 归并为文档级知识计划。"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import PurePosixPath
from typing import Protocol, TypeAlias

from pydantic import ValidationError

from app.config import settings
from app.llm_config import IngestModelProfile
from app.prompts import render_prompt
from app.schemas.ingest_large_document import (
    IngestChunkExtraction,
    IngestKnowledgePlan,
    IngestKnowledgePlanFailure,
    IngestSectionSynthesis,
    IngestSectionSynthesisFailure,
)
from app.services.ingest_context import PromptBudget, TokenEstimator
from app.services.ingest_large_document_status import (
    LargeDocumentProgressCallback,
    LargeDocumentProgressEvent,
)


LOGGER = logging.getLogger(__name__)

DOCUMENT_REDUCE_MAX_OUTPUT_TOKENS = (
    settings.ingest_stage_output_budgets.document_reduce
)
SECTION_SYNTHESIS_MAX_OUTPUT_TOKENS = (
    settings.ingest_stage_output_budgets.section_synthesis
)
SECTION_SYNTHESIS_FAN_IN = 4

_ReduceRecord: TypeAlias = IngestChunkExtraction | IngestSectionSynthesis


class DocumentReduceLLMCaller(Protocol):
    """执行一次已带传输重试策略的 Ingest LLM 调用。"""

    def __call__(self, prompt: str, *, max_tokens: int) -> str: ...


class DocumentReduceError(RuntimeError):
    """结构化 extraction 无法形成可信的文档级知识计划。"""

    def __init__(self, category: str, item_id: str, message: str) -> None:
        self.category = category
        self.item_id = item_id
        super().__init__(f"{category}: {item_id}: {message}")


class _InvalidReduceJSON(ValueError):
    pass


class DocumentKnowledgeReducer:
    """预算允许时直接 Reduce，否则按固定 fan-in 层级归并。"""

    def __init__(
        self,
        *,
        ingest_profile: IngestModelProfile,
        call_llm: DocumentReduceLLMCaller,
        reduce_max_output_tokens: int = DOCUMENT_REDUCE_MAX_OUTPUT_TOKENS,
        section_max_output_tokens: int = SECTION_SYNTHESIS_MAX_OUTPUT_TOKENS,
    ) -> None:
        if reduce_max_output_tokens <= 0 or section_max_output_tokens <= 0:
            raise ValueError("output token limits must be positive")
        self._ingest_profile = ingest_profile
        self._call_llm = call_llm
        profile_limit = min(
            ingest_profile.capabilities.max_output_tokens,
            ingest_profile.llm_profile.max_tokens,
        )
        self._reduce_max_output_tokens = min(
            reduce_max_output_tokens,
            profile_limit,
        )
        self._section_max_output_tokens = min(
            section_max_output_tokens,
            profile_limit,
        )

    def reduce(
        self,
        extractions: tuple[IngestChunkExtraction, ...],
        *,
        document_filename: str,
        today: date,
        job_id: int,
        progress_callback: LargeDocumentProgressCallback | None = None,
    ) -> IngestKnowledgePlan:
        """生成文档级计划，并对覆盖范围与证据引用执行后验校验。"""
        if not extractions:
            raise ValueError("extractions cannot be empty")
        normalized_filename = PurePosixPath(document_filename).name.strip()
        if not normalized_filename:
            raise ValueError("document_filename cannot be blank")

        chunk_ids = tuple(item.chunk_id for item in extractions)
        self._require_unique(chunk_ids, category="reduce_input_chunk_duplicate")
        evidence_owners = self._evidence_owners(extractions)
        if progress_callback is not None:
            progress_callback(
                LargeDocumentProgressEvent(
                    phase="document_reduce",
                    completed=0,
                    total=1,
                    item_id="document-plan",
                )
            )

        direct_prompt = self._render_plan_prompt(
            document_filename=normalized_filename,
            today=today,
            input_type="chunk_extractions",
            records=extractions,
        )
        if self._fits_prompt(
            direct_prompt,
            max_output_tokens=self._reduce_max_output_tokens,
        ):
            LOGGER.info(
                "Document reduce started job_id=%s mode=direct chunks=%s model=%s",
                job_id,
                len(extractions),
                self._ingest_profile.model_identifier,
            )
            plan = self._request_plan(direct_prompt)
            allowed_evidence_ids = set(evidence_owners)
        else:
            LOGGER.info(
                "Document reduce switching to hierarchy job_id=%s chunks=%s fan_in=%s "
                "model=%s",
                job_id,
                len(extractions),
                SECTION_SYNTHESIS_FAN_IN,
                self._ingest_profile.model_identifier,
            )
            records = self._build_hierarchy(
                extractions,
                document_filename=normalized_filename,
                today=today,
                job_id=job_id,
            )
            hierarchical_prompt = self._render_plan_prompt(
                document_filename=normalized_filename,
                today=today,
                input_type="section_syntheses",
                records=records,
            )
            if not self._fits_prompt(
                hierarchical_prompt,
                max_output_tokens=self._reduce_max_output_tokens,
            ):
                raise DocumentReduceError(
                    "reduce_prompt_too_large",
                    str(job_id),
                    "层级归并后的 Document Reduce Prompt 仍超出模型预算",
                )
            plan = self._request_plan(hierarchical_prompt)
            allowed_evidence_ids = self._record_evidence_ids(records)

        self._validate_plan(
            plan,
            expected_chunk_ids=chunk_ids,
            evidence_owners=evidence_owners,
            allowed_evidence_ids=allowed_evidence_ids,
        )
        if progress_callback is not None:
            progress_callback(
                LargeDocumentProgressEvent(
                    phase="document_reduce",
                    completed=1,
                    total=1,
                    item_id="document-plan",
                )
            )
        LOGGER.info(
            "Document reduce completed job_id=%s chunks=%s pages=%s claims=%s",
            job_id,
            len(extractions),
            len(plan.planned_pages),
            len(plan.key_claims),
        )
        return plan

    def _build_hierarchy(
        self,
        records: tuple[_ReduceRecord, ...],
        *,
        document_filename: str,
        today: date,
        job_id: int,
    ) -> tuple[IngestSectionSynthesis, ...]:
        current: tuple[_ReduceRecord, ...] = records
        level = 1
        while True:
            synthesized: list[IngestSectionSynthesis] = []
            input_type = (
                "chunk_extractions"
                if isinstance(current[0], IngestChunkExtraction)
                else "section_syntheses"
            )
            for offset in range(0, len(current), SECTION_SYNTHESIS_FAN_IN):
                group = current[offset : offset + SECTION_SYNTHESIS_FAN_IN]
                ordinal = len(synthesized) + 1
                section_id = f"section-l{level:02d}-{ordinal:04d}"
                prompt = self._render_section_prompt(
                    section_id=section_id,
                    today=today,
                    input_type=input_type,
                    records=group,
                )
                if not self._fits_prompt(
                    prompt,
                    max_output_tokens=self._section_max_output_tokens,
                ):
                    raise DocumentReduceError(
                        "section_prompt_too_large",
                        section_id,
                        "固定 fan-in 分组仍超出模型预算",
                    )
                LOGGER.info(
                    "Section synthesis started job_id=%s section_id=%s level=%s "
                    "records=%s",
                    job_id,
                    section_id,
                    level,
                    len(group),
                )
                result = self._request_section(prompt, section_id=section_id)
                expected_chunks = self._record_chunk_ids(group)
                allowed_evidence = self._record_evidence_ids(group)
                self._validate_section(
                    result,
                    expected_chunk_ids=expected_chunks,
                    allowed_evidence_ids=allowed_evidence,
                )
                synthesized.append(result)
                LOGGER.info(
                    "Section synthesis completed job_id=%s section_id=%s chunks=%s "
                    "claims=%s",
                    job_id,
                    section_id,
                    len(result.covered_chunk_ids),
                    len(result.key_claims),
                )

            next_records = tuple(synthesized)
            final_probe = self._render_plan_prompt(
                document_filename=document_filename,
                today=today,
                input_type="section_syntheses",
                records=next_records,
            )
            if self._fits_prompt(
                final_probe,
                max_output_tokens=self._reduce_max_output_tokens,
            ):
                return next_records
            if len(next_records) == 1:
                return next_records
            current = next_records
            level += 1

    def _request_section(
        self,
        prompt: str,
        *,
        section_id: str,
    ) -> IngestSectionSynthesis:
        parsed = self._request_json(
            prompt,
            item_id=section_id,
            max_output_tokens=self._section_max_output_tokens,
            contract_name="section synthesis",
            identity_requirement=f"section_id must equal {section_id}",
            output_constraints=(
                "Keep the summary concise and retain all covered_chunk_ids; "
                "keep at most 20 key claims, entities, concepts, and conflicts."
            ),
        )
        if parsed.get("synthesis_status") == "failed":
            try:
                failure = IngestSectionSynthesisFailure.model_validate(parsed)
            except ValidationError as exc:
                raise DocumentReduceError(
                    "section_schema_invalid",
                    section_id,
                    "模型失败响应不符合 section synthesis 合约",
                ) from exc
            if failure.section_id != section_id:
                raise DocumentReduceError(
                    "section_identity_mismatch",
                    section_id,
                    "模型失败响应返回了错误的 section_id",
                )
            raise DocumentReduceError(
                "section_synthesis_failed",
                section_id,
                failure.synthesis_error,
            )
        try:
            result = IngestSectionSynthesis.model_validate(parsed)
        except ValidationError as exc:
            raise DocumentReduceError(
                "section_schema_invalid",
                section_id,
                "模型响应不符合 section synthesis 合约",
            ) from exc
        if result.section_id != section_id:
            raise DocumentReduceError(
                "section_identity_mismatch",
                section_id,
                "模型响应返回了错误的 section_id",
            )
        return result

    def _request_plan(self, prompt: str) -> IngestKnowledgePlan:
        parsed = self._request_json(
            prompt,
            item_id="document-plan",
            max_output_tokens=self._reduce_max_output_tokens,
            contract_name="document knowledge plan",
            identity_requirement="planning_status must be succeeded or failed",
            output_constraints=(
                "Keep at most 20 key_claims, 6 planned_pages including the Source "
                "page, 20 contradictions, and 20 supporting evidence IDs per page; "
                "keep document_summary within 1200 characters. Remove repetitive or "
                "lower-value items when necessary, but retain every covered_chunk_id."
            ),
        )
        if parsed.get("planning_status") == "failed":
            try:
                failure = IngestKnowledgePlanFailure.model_validate(parsed)
            except ValidationError as exc:
                raise DocumentReduceError(
                    "reduce_schema_invalid",
                    "document-plan",
                    "模型失败响应不符合 document plan 合约",
                ) from exc
            raise DocumentReduceError(
                "document_reduce_failed",
                "document-plan",
                failure.planning_error,
            )
        try:
            return IngestKnowledgePlan.model_validate(parsed)
        except ValidationError as exc:
            raise DocumentReduceError(
                "reduce_schema_invalid",
                "document-plan",
                "模型响应不符合 document plan 合约",
            ) from exc

    def _request_json(
        self,
        prompt: str,
        *,
        item_id: str,
        max_output_tokens: int,
        contract_name: str,
        identity_requirement: str,
        output_constraints: str,
    ) -> dict[str, object]:
        raw = self._call_model(
            prompt,
            item_id=item_id,
            max_output_tokens=max_output_tokens,
        )
        try:
            return self._parse_json_object(raw)
        except _InvalidReduceJSON:
            repair_prompt = render_prompt(
                "ingest_reduce_repair.md",
                contract_name=contract_name,
                identity_requirement=identity_requirement,
                output_constraints=output_constraints,
                invalid_response=raw,
            )
            repaired = self._call_model(
                repair_prompt,
                item_id=item_id,
                max_output_tokens=max_output_tokens,
            )
            try:
                return self._parse_json_object(repaired)
            except _InvalidReduceJSON as exc:
                raise DocumentReduceError(
                    "reduce_json_invalid",
                    item_id,
                    "模型响应在一次结构修复后仍不是有效 JSON 对象",
                ) from exc

    def _call_model(
        self,
        prompt: str,
        *,
        item_id: str,
        max_output_tokens: int,
    ) -> str:
        if not self._fits_prompt(prompt, max_output_tokens=max_output_tokens):
            raise DocumentReduceError(
                "reduce_prompt_too_large",
                item_id,
                "Prompt 超出当前模型预算",
            )
        try:
            return self._call_llm(prompt, max_tokens=max_output_tokens)
        except Exception as exc:
            raise DocumentReduceError(
                "reduce_llm_call_failed",
                item_id,
                "Document Reduce 模型调用失败",
            ) from exc

    def _fits_prompt(self, prompt: str, *, max_output_tokens: int) -> bool:
        capabilities = self._ingest_profile.capabilities
        budget = PromptBudget(
            max_input_tokens=capabilities.max_input_tokens,
            max_output_tokens=max_output_tokens,
            safety_margin_tokens=capabilities.context_safety_margin_tokens,
            context_window_tokens=capabilities.context_window_tokens,
        )
        return budget.accepts_input(TokenEstimator.estimate(prompt))

    @staticmethod
    def _render_plan_prompt(
        *,
        document_filename: str,
        today: date,
        input_type: str,
        records: tuple[_ReduceRecord, ...],
    ) -> str:
        return render_prompt(
            "ingest_reduce.md",
            document_filename=document_filename,
            today=today.isoformat(),
            reduce_input_type=input_type,
            reduce_items_json=DocumentKnowledgeReducer._records_json(records),
        )

    @staticmethod
    def _render_section_prompt(
        *,
        section_id: str,
        today: date,
        input_type: str,
        records: tuple[_ReduceRecord, ...],
    ) -> str:
        return render_prompt(
            "ingest_section_synthesis.md",
            section_id=section_id,
            today=today.isoformat(),
            input_type=input_type,
            section_items_json=DocumentKnowledgeReducer._records_json(records),
        )

    @staticmethod
    def _records_json(records: tuple[_ReduceRecord, ...]) -> str:
        return json.dumps(
            [record.model_dump(mode="json") for record in records],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _record_chunk_ids(records: tuple[_ReduceRecord, ...]) -> tuple[str, ...]:
        chunk_ids: list[str] = []
        for record in records:
            if isinstance(record, IngestChunkExtraction):
                chunk_ids.append(record.chunk_id)
            else:
                chunk_ids.extend(record.covered_chunk_ids)
        return tuple(chunk_ids)

    @staticmethod
    def _record_evidence_ids(records: tuple[_ReduceRecord, ...]) -> set[str]:
        evidence_ids: set[str] = set()
        for record in records:
            if isinstance(record, IngestChunkExtraction):
                evidence_ids.update(item.evidence_id for item in record.evidence)
            else:
                for claim in record.key_claims:
                    evidence_ids.update(claim.supporting_evidence_ids)
        return evidence_ids

    @staticmethod
    def _evidence_owners(
        extractions: tuple[IngestChunkExtraction, ...],
    ) -> dict[str, str]:
        owners: dict[str, str] = {}
        for extraction in extractions:
            for evidence in extraction.evidence:
                if evidence.evidence_id in owners:
                    raise DocumentReduceError(
                        "reduce_input_evidence_duplicate",
                        evidence.evidence_id,
                        "输入 extraction 包含重复 evidence ID",
                    )
                owners[evidence.evidence_id] = extraction.chunk_id
        return owners

    def _validate_section(
        self,
        section: IngestSectionSynthesis,
        *,
        expected_chunk_ids: tuple[str, ...],
        allowed_evidence_ids: set[str],
    ) -> None:
        self._validate_coverage(
            section.covered_chunk_ids,
            expected_chunk_ids=expected_chunk_ids,
            item_id=section.section_id,
            category="section_chunk_coverage_invalid",
        )
        for claim in section.key_claims:
            self._require_unique(
                tuple(claim.supporting_evidence_ids),
                category="section_evidence_duplicate",
            )
            unknown = set(claim.supporting_evidence_ids) - allowed_evidence_ids
            if unknown:
                raise DocumentReduceError(
                    "section_evidence_unknown",
                    section.section_id,
                    f"section synthesis 引用了未知 evidence ID: {sorted(unknown)}",
                )

    def _validate_plan(
        self,
        plan: IngestKnowledgePlan,
        *,
        expected_chunk_ids: tuple[str, ...],
        evidence_owners: dict[str, str],
        allowed_evidence_ids: set[str],
    ) -> None:
        self._validate_coverage(
            plan.covered_chunk_ids,
            expected_chunk_ids=expected_chunk_ids,
            item_id="document-plan",
            category="reduce_chunk_coverage_invalid",
        )
        if plan.patch_intents:
            raise DocumentReduceError(
                "reduce_patch_intents_premature",
                "document-plan",
                "Wiki 检索前不得猜测已有页面 patch",
            )

        source_pages = [
            page for page in plan.planned_pages if page.page_type == "source"
        ]
        if len(source_pages) != 1:
            raise DocumentReduceError(
                "reduce_source_page_invalid",
                "document-plan",
                "文档计划必须且只能包含一个 Source 页面",
            )
        expected_source_path = f"sources/{plan.slug}.md"
        if source_pages[0].path != expected_source_path:
            raise DocumentReduceError(
                "reduce_source_page_invalid",
                "document-plan",
                f"Source 页面路径必须是 {expected_source_path}",
            )
        seen_paths: set[str] = set()
        seen_titles: set[tuple[str, str]] = set()
        for page in plan.planned_pages:
            normalized_path = page.path.casefold()
            normalized_title = " ".join(page.title.split()).casefold()
            title_key = (page.page_type, normalized_title)
            if normalized_path in seen_paths or title_key in seen_titles:
                raise DocumentReduceError(
                    "reduce_page_duplicate",
                    page.path,
                    "文档计划包含重复页面",
                )
            seen_paths.add(normalized_path)
            seen_titles.add(title_key)
            self._validate_page_path(page.page_type, page.path)
            self._validate_supporting_evidence(
                item_id=page.path,
                chunk_ids=page.supporting_chunk_ids,
                evidence_ids=page.supporting_evidence_ids,
                expected_chunk_ids=expected_chunk_ids,
                evidence_owners=evidence_owners,
                allowed_evidence_ids=allowed_evidence_ids,
            )

    def _validate_supporting_evidence(
        self,
        *,
        item_id: str,
        chunk_ids: list[str],
        evidence_ids: list[str],
        expected_chunk_ids: tuple[str, ...],
        evidence_owners: dict[str, str],
        allowed_evidence_ids: set[str],
    ) -> None:
        self._require_unique(
            tuple(chunk_ids),
            category="reduce_supporting_chunk_duplicate",
        )
        self._require_unique(
            tuple(evidence_ids),
            category="reduce_supporting_evidence_duplicate",
        )
        unknown_chunks = set(chunk_ids) - set(expected_chunk_ids)
        unknown_evidence = set(evidence_ids) - allowed_evidence_ids
        if unknown_chunks:
            raise DocumentReduceError(
                "reduce_supporting_chunk_unknown",
                item_id,
                f"页面引用了未知 chunk ID: {sorted(unknown_chunks)}",
            )
        if unknown_evidence:
            raise DocumentReduceError(
                "reduce_supporting_evidence_unknown",
                item_id,
                f"页面引用了未知 evidence ID: {sorted(unknown_evidence)}",
            )
        cited_chunks = {evidence_owners[evidence_id] for evidence_id in evidence_ids}
        if cited_chunks != set(chunk_ids):
            raise DocumentReduceError(
                "reduce_supporting_evidence_mismatch",
                item_id,
                "页面 supporting chunks 与 evidence 所属 chunk 不一致",
            )

    @staticmethod
    def _validate_page_path(page_type: str, path: str) -> None:
        expected_directory = {
            "source": "sources",
            "entity": "entities",
            "concept": "concepts",
        }[page_type]
        candidate = PurePosixPath(path)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or len(candidate.parts) != 2
            or candidate.parts[0] != expected_directory
            or candidate.suffix != ".md"
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md", candidate.name)
            is None
        ):
            raise DocumentReduceError(
                "reduce_page_path_invalid",
                path,
                f"{page_type} 页面路径无效",
            )

    def _validate_coverage(
        self,
        actual_chunk_ids: list[str],
        *,
        expected_chunk_ids: tuple[str, ...],
        item_id: str,
        category: str,
    ) -> None:
        if len(actual_chunk_ids) != len(set(actual_chunk_ids)):
            raise DocumentReduceError(category, item_id, "chunk 覆盖列表包含重复 ID")
        actual = set(actual_chunk_ids)
        expected = set(expected_chunk_ids)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise DocumentReduceError(
                category,
                item_id,
                f"chunk 覆盖不完整 missing={missing} unknown={unknown}",
            )

    @staticmethod
    def _require_unique(values: tuple[str, ...], *, category: str) -> None:
        if len(values) != len(set(values)):
            raise DocumentReduceError(category, "document-plan", "列表包含重复 ID")

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, object]:
        sanitized = raw.strip()
        if sanitized.startswith("```") and sanitized.endswith("```"):
            sanitized = re.sub(r"^```(?:json)?[ \t]*\r?\n?", "", sanitized)
            sanitized = re.sub(r"\r?\n?```$", "", sanitized).strip()
        try:
            parsed = json.loads(sanitized)
        except json.JSONDecodeError as exc:
            raise _InvalidReduceJSON() from exc
        if not isinstance(parsed, dict):
            raise _InvalidReduceJSON()
        return parsed
