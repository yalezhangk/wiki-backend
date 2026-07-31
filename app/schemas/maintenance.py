from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


MaintenanceTaskKind = Literal["health", "graph", "lint"]
MaintenanceJobStatus = Literal["queued", "running", "succeeded", "failed"]
MaintenanceResultState = Literal["complete", "partial", "unavailable"]
MaintenanceTrigger = Literal["manual", "automatic", "workflow"]
SemanticMode = Literal["agent_compat", "delta", "risk", "full", "selected"]


class MaintenanceJobCreateRequest(BaseModel):
    """创建一个受控的知识库维护任务。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"task_kind": "health", "options": {"save_report": False}},
                {"task_kind": "graph", "options": {"infer_relations": True, "save_report": True}},
                {"task_kind": "lint", "options": {"semantic_analysis": True, "semantic_mode": "agent_compat"}},
                {"task_kind": "lint", "options": {"semantic_analysis": False}},
            ]
        }
    )

    task_kind: MaintenanceTaskKind = Field(description="任务类型：health、graph 或 lint。")
    options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "任务选项。health：save_report（默认 true）；graph：infer_relations（默认 true，会调用 LLM）、"
            "save_report（默认 true）；lint：semantic_analysis（默认 true）、semantic_mode（默认 delta）"
            "和 selected_page_paths（仅 semantic_mode=selected，最多 20 个 Wiki 根目录相对 `.md` 路径）。"
        ),
    )

    @model_validator(mode="after")
    def validate_options(self) -> "MaintenanceJobCreateRequest":
        allowed_options: dict[MaintenanceTaskKind, set[str]] = {
            "health": {"save_report"},
            "graph": {"infer_relations", "save_report"},
            "lint": {"semantic_analysis", "semantic_mode", "selected_page_paths"},
        }
        unknown = set(self.options).difference(allowed_options[self.task_kind])
        if unknown:
            raise ValueError(f"unsupported options: {', '.join(sorted(unknown))}")
        for option_name in {"save_report", "infer_relations", "semantic_analysis"}.intersection(self.options):
            if not isinstance(self.options[option_name], bool):
                raise ValueError(f"{option_name} must be a boolean")
        if "semantic_mode" in self.options and self.options["semantic_mode"] not in {
            "delta",
            "risk",
            "full",
            "selected",
            "agent_compat",
        }:
            raise ValueError("semantic_mode must be agent_compat, delta, risk, full, or selected")
        if self.task_kind != "lint" and "semantic_mode" in self.options:
            raise ValueError("semantic_mode is only supported by lint")
        selected = self.options.get("selected_page_paths")
        if selected is not None and (
            not isinstance(selected, list)
            or len(selected) > 20
            or any(not isinstance(path, str) or not path.strip() for path in selected)
        ):
            raise ValueError("selected_page_paths must contain at most 20 non-empty paths")
        if self.options.get("semantic_mode") == "selected" and not selected:
            raise ValueError("selected mode requires selected_page_paths")
        return self


class MaintenanceWorkflowCreateRequest(BaseModel):
    """创建固定的 health → graph → lint 质量巡检工作流。"""

    model_config = ConfigDict(json_schema_extra={"examples": [{"lint_options": {"semantic_analysis": False}}]})

    lint_options: dict[str, Any] = Field(
        default_factory=dict,
        description="传给工作流末尾 lint 任务的选项；规则与 MaintenanceJobCreateRequest 的 lint options 相同。",
    )

    @model_validator(mode="after")
    def validate_lint_options(self) -> "MaintenanceWorkflowCreateRequest":
        MaintenanceJobCreateRequest(task_kind="lint", options=self.lint_options)
        return self


class MaintenanceJobResponse(BaseModel):
    job_id: int = Field(gt=0, description="任务自增 ID，用于轮询任务详情。")
    task_kind: MaintenanceTaskKind = Field(description="实际执行的任务类型。")
    status: MaintenanceJobStatus = Field(description="生命周期状态：queued、running、succeeded 或 failed。")
    result_state: MaintenanceResultState = Field(description="结果完整性：partial 表示可选 LLM 阶段未完成。")
    trigger: MaintenanceTrigger = Field(description="创建来源：manual、automatic 或 workflow。")
    workflow_id: UUID | None = Field(default=None, description="质量工作流 ID；手动单项任务为 null。")
    depends_on_job_id: int | None = Field(default=None, gt=0, description="前置任务 ID；无依赖时为 null。")
    stage: str = Field(min_length=1, max_length=32, description="当前执行阶段，例如 queued、scanning_pages 或 completed。")
    progress_percent: int = Field(ge=0, le=100, description="任务进度百分比；仅作进度展示，不代表结果完整性。")
    options: dict[str, Any] = Field(default_factory=dict, description="合并默认值后的实际执行选项。")
    result_summary: dict[str, Any] = Field(default_factory=dict, description="任务完成后的结构化摘要；运行中或失败时可能为空。Lint 会以 SHA-256 和字符数记录语义报告审计元数据，不返回模型原文。")
    error: str | None = Field(default=None, description="失败原因的安全截断文本；成功时为 null。")
    created_at: datetime = Field(description="任务创建时间（UTC）。")
    started_at: datetime | None = Field(default=None, description="任务开始执行时间（UTC）；尚未执行时为 null。")
    updated_at: datetime = Field(description="状态或进度最后更新时间（UTC）。")
    finished_at: datetime | None = Field(default=None, description="任务终态时间（UTC）；未完成时为 null。")


class MaintenanceWorkflowResponse(BaseModel):
    workflow_id: UUID = Field(description="本次 quality 工作流的共享 ID。")
    jobs: list[MaintenanceJobResponse] = Field(min_length=3, max_length=3, description="按 health、graph、lint 顺序返回的三个任务。")
