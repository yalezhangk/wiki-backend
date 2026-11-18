from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


IngestEvidenceCategory = Literal[
    "claim",
    "specification",
    "definition",
    "requirement",
    "relationship",
    "warning",
]


class IngestChunkEvidence(BaseModel):
    """一个可回溯到当前 chunk 原文行号的来源证据。"""

    evidence_id: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=300)
    evidence_text: str = Field(min_length=1, max_length=500)
    source_lines: str = Field(pattern=r"^\d+(?:-\d+)?$")
    category: IngestEvidenceCategory

    @field_validator("evidence_id", "statement", "evidence_text")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized


class IngestChunkExtraction(BaseModel):
    """单个 chunk 的来源事实抽取结果，不包含 Wiki 写入规划。"""

    extraction_status: Literal["succeeded"]
    extraction_error: None
    chunk_id: str = Field(min_length=1, max_length=200)
    section_title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=1_000)
    evidence: list[IngestChunkEvidence] = Field(default_factory=list, max_length=20)
    entities: list[str] = Field(default_factory=list, max_length=20)
    concepts: list[str] = Field(default_factory=list, max_length=20)
    internal_conflicts: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("chunk_id", "section_title", "summary")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("entities", "concepts", "internal_conflicts")
    @classmethod
    def normalize_text_list(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = " ".join(value.split())
            if not item:
                continue
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(item)
        return normalized


class IngestChunkExtractionFailure(BaseModel):
    """模型明确声明当前 chunk 无法可靠处理。"""

    extraction_status: Literal["failed"]
    extraction_error: str = Field(min_length=1, max_length=1_000)
    chunk_id: str = Field(min_length=1, max_length=200)


class IngestSynthesizedClaim(BaseModel):
    """层级归并后仍可追溯到原始 evidence 的关键陈述。"""

    statement: str = Field(min_length=1, max_length=1_000)
    supporting_evidence_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("statement")
    @classmethod
    def normalize_statement(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("statement cannot be blank")
        return normalized

    @field_validator("supporting_evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("evidence IDs cannot be blank")
        return normalized


class IngestSectionSynthesis(BaseModel):
    """一组相邻 chunk 或下级 synthesis 的紧凑归并结果。"""

    synthesis_status: Literal["succeeded"]
    synthesis_error: None
    section_id: str = Field(min_length=1, max_length=200)
    covered_chunk_ids: list[str] = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4_000)
    key_claims: list[IngestSynthesizedClaim] = Field(
        default_factory=list,
        max_length=200,
    )
    entities: list[str] = Field(default_factory=list, max_length=100)
    concepts: list[str] = Field(default_factory=list, max_length=100)
    internal_conflicts: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("section_id", "summary")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("covered_chunk_ids")
    @classmethod
    def validate_chunk_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("chunk IDs cannot be blank")
        return normalized


class IngestSectionSynthesisFailure(BaseModel):
    """模型明确声明当前 section 无法可靠归并。"""

    synthesis_status: Literal["failed"]
    synthesis_error: str = Field(min_length=1, max_length=1_000)
    section_id: str = Field(min_length=1, max_length=200)


class IngestPlannedPage(BaseModel):
    """由来源证据支持、等待后续逐页生成的 Wiki 页面计划。"""

    page_type: Literal["source", "entity", "concept"]
    title: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=500)
    purpose: str = Field(min_length=1, max_length=300)
    supporting_chunk_ids: list[str] = Field(min_length=1, max_length=20)
    supporting_evidence_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("title", "path", "purpose")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("supporting_chunk_ids", "supporting_evidence_ids")
    @classmethod
    def validate_required_id_list(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("ID list cannot contain blank values")
        return normalized


class IngestPatchIntent(BaseModel):
    """等待 Wiki 检索阶段确认的 Entity 或 Concept 更新意图。"""

    page_type: Literal["entity", "concept"]
    existing_path: str = Field(min_length=1, max_length=500)
    purpose: str = Field(min_length=1, max_length=1_000)
    supporting_chunk_ids: list[str] = Field(min_length=1, max_length=200)
    supporting_evidence_ids: list[str] = Field(min_length=1, max_length=500)

    @field_validator("existing_path", "purpose")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("supporting_chunk_ids", "supporting_evidence_ids")
    @classmethod
    def validate_required_id_list(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("ID list cannot contain blank values")
        return normalized


class IngestKnowledgePlan(BaseModel):
    """文档级 Reduce 产生的来源知识计划。"""

    planning_status: Literal["succeeded"]
    planning_error: None
    title: str = Field(min_length=1, max_length=200)
    slug: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    document_summary: str = Field(min_length=1, max_length=1_200)
    key_claims: list[str] = Field(default_factory=list, max_length=20)
    covered_chunk_ids: list[str] = Field(min_length=1, max_length=500)
    planned_pages: list[IngestPlannedPage] = Field(min_length=1, max_length=6)
    patch_intents: list[IngestPatchIntent] = Field(default_factory=list, max_length=6)
    contradictions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("title", "document_summary")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("covered_chunk_ids")
    @classmethod
    def validate_chunk_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("chunk IDs cannot be blank")
        return normalized


class IngestKnowledgePlanFailure(BaseModel):
    """模型明确声明无法形成可信的文档级计划。"""

    planning_status: Literal["failed"]
    planning_error: str = Field(min_length=1, max_length=1_000)


class IngestGeneratedSourcePage(BaseModel):
    """Source 页面单独生成调用的成功响应。"""

    generation_status: Literal["succeeded"]
    generation_error: None
    target_id: Literal["source"]
    title: str = Field(min_length=1, max_length=200)
    slug: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    content: str = Field(min_length=1)


class IngestGeneratedKnowledgeCreate(BaseModel):
    """单个 Entity/Concept 新页面生成结果。"""

    generation_status: Literal["succeeded"]
    generation_error: None
    target_id: str = Field(min_length=1, max_length=500)
    action: Literal["create"]
    page_type: Literal["entity", "concept"]
    title: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    index_summary: str = Field(min_length=1, max_length=500)


class IngestGeneratedKnowledgePatch(BaseModel):
    """单个已检索 Entity/Concept 页面的受控 patch 生成结果。"""

    generation_status: Literal["succeeded"]
    generation_error: None
    target_id: str = Field(min_length=1, max_length=500)
    action: Literal["patch"]
    page_type: Literal["entity", "concept"]
    path: str = Field(min_length=1, max_length=500)
    base_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    operation: Literal["append_section", "replace_section"]
    heading: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)


class IngestGeneratedKnowledgeSkip(BaseModel):
    """证据不足或既有内容无需变更时的显式跳过响应。"""

    generation_status: Literal["succeeded"]
    generation_error: None
    target_id: str = Field(min_length=1, max_length=500)
    action: Literal["skip"]
    page_type: Literal["entity", "concept"]
    reason: str = Field(min_length=1, max_length=1_000)


class IngestPageGenerationFailure(BaseModel):
    """模型明确声明单个页面无法可靠生成。"""

    generation_status: Literal["failed"]
    generation_error: str = Field(min_length=1, max_length=1_000)
    target_id: str = Field(min_length=1, max_length=500)
