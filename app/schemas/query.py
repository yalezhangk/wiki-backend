from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

NonEmptyQuestion = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
CitationKind = Literal["source", "entity", "concept", "synthesis", "page"]


class CitationResponse(BaseModel):
    """可由 Quartz 稳定打开的结构化 Wiki 引用。"""

    path: str = Field(description="Wiki 根目录相对路径，统一使用 `/` 分隔符。")
    title: str = Field(description="来自 frontmatter、一级标题或文件名的页面标题。")
    kind: CitationKind = Field(description="知识对象类型；无法确定时为 `page`。")
    excerpt: str | None = Field(default=None, description="真实命中片段；当前检索层暂不提供。")
    relevance: float | None = Field(default=None, description="真实相关度分数；当前检索层暂不提供。")


class QueryRequest(BaseModel):
    """单轮问答请求体。"""

    question: NonEmptyQuestion = Field(
        description="用户提问内容。该问题会直接送入知识库检索和 LLM 推理，不保存为聊天历史。",
        examples=["这个项目的核心功能是什么？"],
    )


class QueryResponse(BaseModel):
    """单轮问答响应体。"""

    answer: str = Field(description="LLM 生成的最终回答。")
    sources: list[str] = Field(
        default_factory=list,
        description="按检索顺序稳定去重的 Wiki 根目录相对来源路径；正文 `[n]` 对应 `sources[n - 1]`。",
        examples=[["sources/产品说明.md", "entities/Smart HVX.md"]],
    )
    relevant_pages: list[str] = Field(
        default_factory=list,
        description="检索阶段使用的 Wiki 根目录相对路径列表，统一使用 `/` 分隔符。",
        examples=[["overview.md", "entities/Smart HVX.md"]],
    )
    citations: list[CitationResponse] = Field(
        default_factory=list,
        description="可展示并打开的结构化引用；保留旧引用字段用于兼容。",
    )


@dataclass(frozen=True)
class QueryResult:
    answer: str
    sources: list[str]
    relevant_pages: list[str]
    citations: list[CitationResponse] = field(default_factory=list)
