from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.publish import PublicationResponse

IngestJobStatus = Literal["queued", "running", "succeeded", "failed"]
IngestTrigger = Literal["manual", "scheduled"]
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


class IngestIndexEntry(BaseModel):
    """新建 Entity 或 Concept 在对应索引分区中的条目。"""

    path: str
    entry: str = Field(min_length=1)


class IngestPagePatch(BaseModel):
    """对检索到的既有知识页执行的受控小节修改。"""

    path: str
    base_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    operation: Literal["append_section", "replace_section"]
    heading: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)


class IngestLLMResult(BaseModel):
    ingest_status: Literal["succeeded"]
    ingest_error: None
    title: str = Field(min_length=1, max_length=200)
    slug: str
    source_page: str
    index_entry: str
    overview_update: str | None = None
    entity_pages: list[IngestGeneratedPage] = Field(default_factory=list)
    concept_pages: list[IngestGeneratedPage] = Field(default_factory=list)
    entity_index_entries: list[IngestIndexEntry] = Field(default_factory=list)
    concept_index_entries: list[IngestIndexEntry] = Field(default_factory=list)
    entity_patches: list[IngestPagePatch] = Field(default_factory=list)
    concept_patches: list[IngestPagePatch] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    log_entry: str

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("title cannot be blank")
        return normalized


class IngestLLMFailure(BaseModel):
    ingest_status: Literal["failed"]
    ingest_error: str = Field(min_length=1, max_length=1000)


class IngestJobResponse(BaseModel):
    job_id: int = Field(gt=0, description="Ingest 任务数字 ID。")
    status: IngestJobStatus = Field(description="任务粗粒度状态；成功仅表示知识文件已写入。")
    stage: IngestStage = Field(description="任务当前真实处理阶段；不包含 Quartz 发布阶段。")
    progress_percent: int = Field(ge=0, le=100, description="当前阶段对应的离散进度，不表示发布进度。")
    original_filename: str = Field(description="上传时的原始文件名，不包含客户端目录。")
    trigger: IngestTrigger = Field(
        default="manual",
        description="任务来源；manual 表示人工上传，scheduled 表示 DGX 定时同步。",
    )
    source_path: str = Field(description="相对于 agent 仓库根目录的上传源文件路径。")
    document_name_key: str | None = Field(
        default=None,
        description="后端生成的全局文档主名唯一键；失败任务会释放该键。",
    )
    source_url: str | None = Field(
        default=None,
        description="scheduled 任务对应的 http/https 原始文档 URL；manual 任务恒为 null。",
    )
    ingest_model: str | None = Field(
        default=None,
        description="创建任务时选定的服务端 Ingest 模型标识；历史任务可能为空。",
    )
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
    created_at: datetime = Field(description="任务创建时间，北京时间、秒精度。")
    started_at: datetime | None = Field(default=None, description="任务开始时间，北京时间、秒精度。")
    updated_at: datetime = Field(description="任务状态或阶段最近更新时间，北京时间、秒精度。")
    finished_at: datetime | None = Field(default=None, description="任务结束时间，北京时间、秒精度。")
    publication: PublicationResponse | None = Field(
        default=None,
        description="站点发布状态；仅在知识写入成功后存在。",
    )
