"""大文档 Ingest 的逐 chunk 来源证据抽取。"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Protocol

from pydantic import ValidationError

from app.config import settings
from app.llm_config import IngestModelProfile
from app.prompts import render_prompt
from app.schemas.ingest_large_document import (
    IngestChunkEvidence,
    IngestChunkExtraction,
    IngestChunkExtractionFailure,
)
from app.services.ingest_context import PromptBudget, TokenEstimator
from app.services.ingest_large_document import MarkdownChunk
from app.services.ingest_large_document_status import (
    LargeDocumentProgressCallback,
    LargeDocumentProgressEvent,
)


LOGGER = logging.getLogger(__name__)

CHUNK_EXTRACTION_MAX_OUTPUT_TOKENS = (
    settings.ingest_stage_output_budgets.chunk_extraction
)
_EVIDENCE_ID_SUFFIX_PATTERN = re.compile(r"^e\d{3,}$")
_SOURCE_LINES_PATTERN = re.compile(r"^(?P<start>\d+)(?:-(?P<end>\d+))?$")
_REPAIRABLE_EVIDENCE_CATEGORIES = frozenset(
    {
        "chunk_evidence_id_invalid",
        "chunk_evidence_id_duplicate",
        "chunk_evidence_lines_invalid",
        "chunk_evidence_not_grounded",
    }
)


class ChunkLLMCaller(Protocol):
    """执行一次已带传输重试策略的 Ingest LLM 调用。"""

    def __call__(self, prompt: str, *, max_tokens: int) -> str: ...


class ChunkEvidenceExtractionError(RuntimeError):
    """单个 chunk 无法形成可信的结构化证据。"""

    def __init__(self, category: str, chunk_id: str, message: str) -> None:
        self.category = category
        self.chunk_id = chunk_id
        super().__init__(f"{category}: {chunk_id}: {message}")


class _InvalidChunkJSON(ValueError):
    pass


class ChunkEvidenceExtractor:
    """顺序调用模型并验证每个 chunk 的来源证据。"""

    def __init__(
        self,
        *,
        ingest_profile: IngestModelProfile,
        call_llm: ChunkLLMCaller,
        max_output_tokens: int = CHUNK_EXTRACTION_MAX_OUTPUT_TOKENS,
    ) -> None:
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self._ingest_profile = ingest_profile
        self._call_llm = call_llm
        self._max_output_tokens = min(
            max_output_tokens,
            ingest_profile.capabilities.max_output_tokens,
            ingest_profile.llm_profile.max_tokens,
        )

    def extract(
        self,
        chunks: tuple[MarkdownChunk, ...],
        *,
        today: date,
        progress_callback: LargeDocumentProgressCallback | None = None,
    ) -> tuple[IngestChunkExtraction, ...]:
        """按来源顺序完成全部 chunk；任一失败立即停止。"""
        if not chunks:
            raise ValueError("chunks cannot be empty")

        results: list[IngestChunkExtraction] = []
        known_evidence_ids: set[str] = set()
        total = len(chunks)
        if progress_callback is not None:
            progress_callback(
                LargeDocumentProgressEvent(
                    phase="chunk_extraction",
                    completed=0,
                    total=total,
                )
            )
        for position, chunk in enumerate(chunks, start=1):
            LOGGER.info(
                "Chunk evidence extraction started chunk_id=%s position=%s total=%s "
                "estimated_tokens=%s model=%s",
                chunk.chunk_id,
                position,
                total,
                chunk.estimated_tokens,
                self._ingest_profile.model_identifier,
            )
            try:
                result = self._extract_chunk(chunk, today=today)
                try:
                    self._validate_evidence(
                        chunk,
                        result,
                        known_evidence_ids=known_evidence_ids,
                    )
                except ChunkEvidenceExtractionError as exc:
                    if exc.category not in _REPAIRABLE_EVIDENCE_CATEGORIES:
                        raise
                    LOGGER.warning(
                        "Chunk evidence grounding invalid; repairing once "
                        "chunk_id=%s category=%s",
                        chunk.chunk_id,
                        exc.category,
                    )
                    result = self._repair_evidence_grounding(
                        chunk,
                        invalid_response=result.model_dump_json(),
                        validation_category=exc.category,
                    )
                    result = self._filter_repaired_evidence(
                        chunk,
                        result,
                        known_evidence_ids=known_evidence_ids,
                    )
                    self._validate_evidence(
                        chunk,
                        result,
                        known_evidence_ids=known_evidence_ids,
                    )
            except ChunkEvidenceExtractionError:
                LOGGER.warning(
                    "Chunk evidence extraction failed chunk_id=%s position=%s total=%s",
                    chunk.chunk_id,
                    position,
                    total,
                    exc_info=True,
                )
                raise
            results.append(result)
            known_evidence_ids.update(item.evidence_id for item in result.evidence)
            if progress_callback is not None:
                progress_callback(
                    LargeDocumentProgressEvent(
                        phase="chunk_extraction",
                        completed=position,
                        total=total,
                        item_id=chunk.chunk_id,
                    )
                )
            LOGGER.info(
                "Chunk evidence extraction completed chunk_id=%s position=%s total=%s "
                "evidence_count=%s",
                chunk.chunk_id,
                position,
                total,
                len(result.evidence),
            )
        return tuple(results)

    def _extract_chunk(
        self,
        chunk: MarkdownChunk,
        *,
        today: date,
    ) -> IngestChunkExtraction:
        prompt = render_prompt(
            "ingest_chunk.md",
            chunk_id=chunk.chunk_id,
            source_line_range=f"{chunk.start_line}-{chunk.end_line}",
            breadcrumbs=" / ".join(chunk.breadcrumbs) or "(document root)",
            today=today.isoformat(),
            overlap_text=chunk.overlap_text or "(none)",
            structural_context=chunk.structural_context or "(none)",
            primary_text=self._number_primary_text(chunk),
        )
        raw = self._call_model(prompt, chunk_id=chunk.chunk_id)
        try:
            parsed = self._parse_json_object(raw)
        except _InvalidChunkJSON:
            repair_prompt = render_prompt(
                "ingest_chunk_repair.md",
                chunk_id=chunk.chunk_id,
                invalid_response=raw,
            )
            repaired_raw = self._call_model(repair_prompt, chunk_id=chunk.chunk_id)
            try:
                parsed = self._parse_json_object(repaired_raw)
            except _InvalidChunkJSON as exc:
                raise ChunkEvidenceExtractionError(
                    "chunk_json_invalid",
                    chunk.chunk_id,
                    "模型响应在一次结构修复后仍不是有效 JSON 对象",
                ) from exc

        if parsed.get("extraction_status") == "failed":
            try:
                failure = IngestChunkExtractionFailure.model_validate(parsed)
            except ValidationError as exc:
                raise ChunkEvidenceExtractionError(
                    "chunk_schema_invalid",
                    chunk.chunk_id,
                    "模型失败响应不符合 chunk extraction 合约",
                ) from exc
            if failure.chunk_id != chunk.chunk_id:
                raise ChunkEvidenceExtractionError(
                    "chunk_identity_mismatch",
                    chunk.chunk_id,
                    "模型失败响应返回了错误的 chunk_id",
                )
            raise ChunkEvidenceExtractionError(
                "chunk_extraction_failed",
                chunk.chunk_id,
                failure.extraction_error,
            )

        try:
            result = IngestChunkExtraction.model_validate(parsed)
        except ValidationError as exc:
            LOGGER.warning(
                "Chunk extraction schema invalid; repairing once chunk_id=%s",
                chunk.chunk_id,
            )
            result = self._repair_evidence_grounding(
                chunk,
                invalid_response=json.dumps(
                    parsed,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                validation_category="chunk_schema_invalid",
            )
            result = self._filter_repaired_evidence(
                chunk,
                result,
                known_evidence_ids=set(),
            )
        if result.chunk_id != chunk.chunk_id:
            raise ChunkEvidenceExtractionError(
                "chunk_identity_mismatch",
                chunk.chunk_id,
                "模型响应返回了错误的 chunk_id",
            )
        return result

    def _repair_evidence_grounding(
        self,
        chunk: MarkdownChunk,
        *,
        invalid_response: str,
        validation_category: str,
    ) -> IngestChunkExtraction:
        prompt = render_prompt(
            "ingest_chunk_grounding_repair.md",
            chunk_id=chunk.chunk_id,
            source_line_range=f"{chunk.start_line}-{chunk.end_line}",
            validation_category=validation_category,
            primary_text=self._number_primary_text(chunk),
            invalid_response=invalid_response,
        )
        raw = self._call_model(prompt, chunk_id=chunk.chunk_id)
        try:
            parsed = self._parse_json_object(raw)
        except _InvalidChunkJSON as exc:
            raise ChunkEvidenceExtractionError(
                "chunk_grounding_repair_json_invalid",
                chunk.chunk_id,
                "证据修复响应不是有效 JSON 对象",
            ) from exc
        for field_name in ("entities", "concepts", "internal_conflicts"):
            values = parsed.get(field_name)
            if isinstance(values, list) and len(values) > 20:
                LOGGER.warning(
                    "Truncated oversized chunk repair list chunk_id=%s field=%s count=%s",
                    chunk.chunk_id,
                    field_name,
                    len(values),
                )
                parsed[field_name] = values[:20]
        try:
            repaired = IngestChunkExtraction.model_validate(parsed)
        except ValidationError as exc:
            raise ChunkEvidenceExtractionError(
                "chunk_grounding_repair_schema_invalid",
                chunk.chunk_id,
                "证据修复响应不符合 chunk extraction 合约",
            ) from exc
        if repaired.chunk_id != chunk.chunk_id:
            raise ChunkEvidenceExtractionError(
                "chunk_identity_mismatch",
                chunk.chunk_id,
                "证据修复响应返回了错误的 chunk_id",
            )
        return repaired

    def _filter_repaired_evidence(
        self,
        chunk: MarkdownChunk,
        result: IngestChunkExtraction,
        *,
        known_evidence_ids: set[str],
    ) -> IngestChunkExtraction:
        if not result.evidence:
            return result
        valid: list[IngestChunkEvidence] = []
        accepted_ids = set(known_evidence_ids)
        for evidence in result.evidence:
            candidate = result.model_copy(update={"evidence": [evidence]})
            try:
                self._validate_evidence(
                    chunk,
                    candidate,
                    known_evidence_ids=accepted_ids,
                )
            except ChunkEvidenceExtractionError as exc:
                LOGGER.warning(
                    "Dropping invalid evidence after one grounding repair "
                    "chunk_id=%s evidence_id=%s category=%s",
                    chunk.chunk_id,
                    evidence.evidence_id,
                    exc.category,
                )
                continue
            valid.append(evidence)
            accepted_ids.add(evidence.evidence_id)
        if not valid:
            raise ChunkEvidenceExtractionError(
                "chunk_evidence_repair_empty",
                chunk.chunk_id,
                "证据修复后没有任何可回溯证据",
            )
        return result.model_copy(update={"evidence": valid})

    def _call_model(self, prompt: str, *, chunk_id: str) -> str:
        self._assert_prompt_budget(prompt, chunk_id=chunk_id)
        try:
            return self._call_llm(prompt, max_tokens=self._max_output_tokens)
        except Exception as exc:
            raise ChunkEvidenceExtractionError(
                "chunk_llm_call_failed",
                chunk_id,
                "chunk extraction 模型调用失败",
            ) from exc

    def _assert_prompt_budget(self, prompt: str, *, chunk_id: str) -> None:
        capabilities = self._ingest_profile.capabilities
        input_tokens = TokenEstimator.estimate(prompt)
        budget = PromptBudget(
            max_input_tokens=capabilities.max_input_tokens,
            max_output_tokens=self._max_output_tokens,
            safety_margin_tokens=capabilities.context_safety_margin_tokens,
            context_window_tokens=capabilities.context_window_tokens,
        )
        if not budget.accepts_input(input_tokens):
            raise ChunkEvidenceExtractionError(
                "chunk_prompt_too_large",
                chunk_id,
                "chunk extraction Prompt 超出当前模型预算",
            )
        LOGGER.info(
            "Chunk extraction prompt budget accepted chunk_id=%s input_tokens=%s "
            "max_output_tokens=%s model=%s",
            chunk_id,
            input_tokens,
            self._max_output_tokens,
            self._ingest_profile.model_identifier,
        )

    def _validate_evidence(
        self,
        chunk: MarkdownChunk,
        result: IngestChunkExtraction,
        *,
        known_evidence_ids: set[str],
    ) -> None:
        local_evidence_ids: set[str] = set()
        for evidence in result.evidence:
            expected_prefix = f"{chunk.chunk_id}:"
            suffix = evidence.evidence_id.removeprefix(expected_prefix)
            if (
                not evidence.evidence_id.startswith(expected_prefix)
                or _EVIDENCE_ID_SUFFIX_PATTERN.fullmatch(suffix) is None
            ):
                raise ChunkEvidenceExtractionError(
                    "chunk_evidence_id_invalid",
                    chunk.chunk_id,
                    f"证据 ID 不符合 {chunk.chunk_id}:eNNN 格式",
                )
            if (
                evidence.evidence_id in local_evidence_ids
                or evidence.evidence_id in known_evidence_ids
            ):
                raise ChunkEvidenceExtractionError(
                    "chunk_evidence_id_duplicate",
                    chunk.chunk_id,
                    "证据 ID 重复",
                )
            local_evidence_ids.add(evidence.evidence_id)

            start_line, end_line = self._parse_source_lines(
                evidence.source_lines,
                chunk_id=chunk.chunk_id,
            )
            if start_line < chunk.start_line or end_line > chunk.end_line:
                raise ChunkEvidenceExtractionError(
                    "chunk_evidence_lines_invalid",
                    chunk.chunk_id,
                    "证据行号超出当前 chunk 的主来源范围",
                )
            source_excerpt = self._source_lines_text(
                chunk,
                start_line=start_line,
                end_line=end_line,
            )
            normalized_evidence = self._normalize_grounding_text(evidence.evidence_text)
            normalized_source = self._normalize_grounding_text(source_excerpt)
            if normalized_evidence not in normalized_source:
                raise ChunkEvidenceExtractionError(
                    "chunk_evidence_not_grounded",
                    chunk.chunk_id,
                    "evidence_text 无法在声明的来源行中找到",
                )

    @staticmethod
    def _parse_source_lines(source_lines: str, *, chunk_id: str) -> tuple[int, int]:
        match = _SOURCE_LINES_PATTERN.fullmatch(source_lines)
        if match is None:
            raise ChunkEvidenceExtractionError(
                "chunk_evidence_lines_invalid",
                chunk_id,
                "证据行号格式无效",
            )
        start_line = int(match.group("start"))
        end_line = int(match.group("end") or start_line)
        if start_line <= 0 or end_line < start_line:
            raise ChunkEvidenceExtractionError(
                "chunk_evidence_lines_invalid",
                chunk_id,
                "证据行号范围无效",
            )
        return start_line, end_line

    @staticmethod
    def _source_lines_text(
        chunk: MarkdownChunk,
        *,
        start_line: int,
        end_line: int,
    ) -> str:
        lines = chunk.primary_text.splitlines()
        local_start = start_line - chunk.start_line
        local_end = end_line - chunk.start_line + 1
        return "\n".join(lines[local_start:local_end])

    @staticmethod
    def _normalize_grounding_text(value: str) -> str:
        # Markdown 表格的竖线只表达单元格边界；模型可引用相邻单元格文字，
        # 但不能要求它把渲染标记本身复制进 evidence_text。
        return " ".join(value.replace("|", " ").split())

    @staticmethod
    def _number_primary_text(chunk: MarkdownChunk) -> str:
        """为模型显式标出绝对来源行号，不改变后验校验使用的原文。"""
        return "\n".join(
            f"L{line_number}: {line}"
            for line_number, line in enumerate(
                chunk.primary_text.splitlines(),
                start=chunk.start_line,
            )
        )

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, object]:
        sanitized = raw.strip()
        if sanitized.startswith("```") and sanitized.endswith("```"):
            sanitized = re.sub(r"^```(?:json)?[ \t]*\r?\n?", "", sanitized)
            sanitized = re.sub(r"\r?\n?```$", "", sanitized).strip()
        try:
            parsed = json.loads(sanitized)
        except json.JSONDecodeError as exc:
            if exc.msg != "Invalid \\escape":
                raise _InvalidChunkJSON() from exc
            escaped_tex = re.sub(
                r"(?<!\\)\\(?=[A-Za-z]{2,})",
                r"\\\\",
                sanitized,
            )
            if escaped_tex == sanitized:
                raise _InvalidChunkJSON() from exc
            try:
                parsed = json.loads(escaped_tex)
            except json.JSONDecodeError as repair_error:
                raise _InvalidChunkJSON() from repair_error
        if not isinstance(parsed, dict):
            raise _InvalidChunkJSON()
        return parsed
