from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

NonEmptyQuestion = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


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
        description="回答所引用的来源文档路径或文件名列表。",
        examples=[["wiki/index.md", "wiki/overview.md"]],
    )
    relevant_pages: list[str] = Field(
        default_factory=list,
        description="检索阶段判断为与问题最相关的页面列表。",
        examples=[["index", "overview"]],
    )


@dataclass(frozen=True)
class QueryResult:
    answer: str
    sources: list[str]
    relevant_pages: list[str]
