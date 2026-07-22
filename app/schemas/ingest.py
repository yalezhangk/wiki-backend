from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

IngestJobStatus = Literal["queued", "running", "succeeded", "failed"]
IngestStage = Literal[
    "uploaded",
    "converting",
    "extracting",
    "writing_wiki",
    "validating",
    "completed",
]


class IngestValidation(BaseModel):
    broken_links: list[tuple[str, str]] = Field(
        default_factory=list,
        description="断链列表；每项依次为包含断链的 Wiki 相对路径和无法解析的 wikilink 标识。",
    )
    unindexed: list[str] = Field(
        default_factory=list,
        description="未出现在 Wiki 索引中的 Wiki 根目录相对路径。",
    )


class IngestGeneratedPage(BaseModel):
    path: str
    content: str


class IngestLLMResult(BaseModel):
    title: str
    slug: str
    source_page: str
    index_entry: str
    overview_update: str | None = None
    entity_pages: list[IngestGeneratedPage] = Field(default_factory=list)
    concept_pages: list[IngestGeneratedPage] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    log_entry: str


class IngestJobResponse(BaseModel):
    job_id: str = Field(description="Ingest 任务 ID。")
    status: IngestJobStatus = Field(description="任务粗粒度状态；成功仅表示知识文件已写入。")
    stage: IngestStage = Field(description="任务当前真实处理阶段；不包含 Quartz 发布阶段。")
    progress_percent: int = Field(ge=0, le=100, description="当前阶段对应的离散进度，不表示发布进度。")
    original_filename: str = Field(description="上传时的原始文件名，不包含客户端目录。")
    source_path: str = Field(description="相对于 agent 仓库根目录的上传源文件路径。")
    created_pages: list[str] = Field(
        default_factory=list,
        description="本任务创建的 Wiki 根目录相对路径列表。",
    )
    updated_pages: list[str] = Field(
        default_factory=list,
        description="本任务更新的 Wiki 根目录相对路径列表。",
    )
    contradictions: list[str] = Field(default_factory=list, description="入库结果报告的矛盾摘要。")
    validation: IngestValidation = Field(default_factory=IngestValidation, description="入库后的校验摘要。")
    error: str | None = Field(default=None, description="失败原因；非失败状态为 null。")
    created_at: datetime = Field(description="任务创建时间，UTC、秒精度。")
    started_at: datetime | None = Field(default=None, description="任务开始时间，UTC、秒精度。")
    updated_at: datetime = Field(description="任务状态或阶段最近更新时间，UTC、秒精度。")
    finished_at: datetime | None = Field(default=None, description="任务结束时间，UTC、秒精度。")
