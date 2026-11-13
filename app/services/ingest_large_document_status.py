"""大文档内部阶段的进度事件与安全错误语义。"""
from __future__ import annotations

import re
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


LargeDocumentPhase = Literal[
    "chunking",
    "chunk_extraction",
    "document_reduce",
    "wiki_retrieval",
    "page_generation",
    "result_assembly",
]

_PHASE_PROGRESS_RANGES: dict[LargeDocumentPhase, tuple[int, int]] = {
    "chunking": (36, 38),
    "chunk_extraction": (39, 49),
    "document_reduce": (50, 54),
    "wiki_retrieval": (55, 57),
    "page_generation": (58, 63),
    "result_assembly": (64, 64),
}

_PHASE_USER_MESSAGES: dict[LargeDocumentPhase, str] = {
    "chunking": "大文档无法安全分块。",
    "chunk_extraction": "大文档的分块证据抽取失败。",
    "document_reduce": "大文档无法形成可信的文档级知识计划。",
    "wiki_retrieval": "无法读取大文档所需的 Wiki 上下文。",
    "page_generation": "大文档的单页内容生成失败。",
    "result_assembly": "大文档生成结果无法安全组装。",
}


class LargeDocumentProgressEvent(BaseModel):
    """不包含来源正文的单个内部阶段进度事件。"""

    model_config = ConfigDict(frozen=True)

    phase: LargeDocumentPhase
    completed: int = Field(ge=0)
    total: int = Field(gt=0)
    item_id: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_completed_not_above_total(self) -> LargeDocumentProgressEvent:
        if self.completed > self.total:
            raise ValueError("completed cannot exceed total")
        return self

    @property
    def progress_percent(self) -> int:
        """映射到现有 extracting 阶段保留的 35–64% 区间。"""
        start, end = _PHASE_PROGRESS_RANGES[self.phase]
        if start == end:
            return end
        return start + ((end - start) * self.completed // self.total)


LargeDocumentProgressCallback = Callable[[LargeDocumentProgressEvent], None]


class LargeDocumentLLMCaller(Protocol):
    """执行一次带既有传输重试策略的分阶段 Ingest 模型调用。"""

    def __call__(self, prompt: str, *, max_tokens: int) -> str: ...


class LargeDocumentStageError(RuntimeError):
    """可安全暴露 category，同时保留内部阶段和对象标识供日志使用。"""

    def __init__(
        self,
        *,
        phase: LargeDocumentPhase,
        category: str,
        item_id: str,
    ) -> None:
        self.phase = phase
        self.category = (
            category
            if re.fullmatch(r"[a-z0-9_]{1,100}", category)
            else f"large_document_{phase}_failed"
        )
        self.item_id = _normalize_item_id(item_id)
        self.user_message = _PHASE_USER_MESSAGES[phase]
        super().__init__(f"{self.category}: {self.user_message}")


def normalize_large_document_error(
    *,
    phase: LargeDocumentPhase,
    error: Exception,
    item_id: str | None = None,
) -> LargeDocumentStageError:
    """把阶段内部异常转换成不包含模型响应或来源正文的稳定错误。"""
    category_value = getattr(error, "category", None)
    category = (
        category_value
        if isinstance(category_value, str)
        and re.fullmatch(r"[a-z0-9_]{1,100}", category_value)
        else f"large_document_{phase}_failed"
    )
    resolved_item_id = _normalize_item_id(
        item_id or _error_item_id(error) or phase
    )
    return LargeDocumentStageError(
        phase=phase,
        category=category,
        item_id=resolved_item_id,
    )


def _error_item_id(error: Exception) -> str | None:
    for attribute in ("chunk_id", "item_id", "target_id"):
        value = getattr(error, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _normalize_item_id(value: str) -> str:
    normalized = " ".join(value.split())
    return (normalized or "unknown")[:500]
