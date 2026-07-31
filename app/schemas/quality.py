from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

QualityState = Literal["available", "stale", "missing", "parse_failed", "not_run", "incomplete"]


class QualityCheckResponse(BaseModel):
    state: QualityState = Field(description="报告状态：available、stale、missing、parse_failed、not_run 或 incomplete。")
    generated_at: datetime | None = Field(default=None, description="对应报告生成或修改时间（UTC）；没有报告时为 null。")
    message: str = Field(description="面向调用方的当前状态说明。")


class QualityCoverageResponse(BaseModel):
    checked_object_count: int = Field(ge=0, description="最近语义 Lint 实际检查的页面数；不可用时为 0。")
    scope: Literal["sampled", "full", "unknown"] = Field(description="检查覆盖范围；当前无法判断时为 unknown。")


class QualitySnapshotResponse(BaseModel):
    status: QualityState = Field(description="总体快照状态；至少一项核心报告可用时为 available。")
    generated_at: datetime | None = Field(default=None, description="各检查项中最近的报告时间（UTC）；没有可用报告时为 null。")
    current_object_count: int = Field(ge=0, description="当前 Wiki 中纳入检查的 Markdown 页面数量。")
    coverage: QualityCoverageResponse = Field(description="最近语义检查的覆盖信息。")
    checks: dict[str, QualityCheckResponse] = Field(description="按 health、lint、graph、freshness 分类的报告状态。")


class QualityFindingResponse(BaseModel):
    id: str = Field(description="快照内稳定的 finding 标识。")
    category: Literal["consistency", "structure", "graph", "freshness"] = Field(description="finding 所属质量维度。")
    severity: Literal["critical", "warning", "info", "unknown"] = Field(default="unknown", description="来源报告给出的严重级别；无法判断时为 unknown。")
    status: Literal["needs_review", "documented_difference", "unavailable"] = Field(default="unavailable", description="人工复核状态。")
    title: str = Field(description="finding 的简短标题。")
    summary: str = Field(description="finding 的摘要说明。")
    pages: list[str] = Field(default_factory=list, description="关联 Wiki 相对页面路径，不含本机绝对路径。")
    evidence: list[dict[str, str]] = Field(default_factory=list, max_length=2, description="最多两项报告证据摘录。")
    recommendation: str | None = Field(default=None, description="可选的后续处理建议。")
    report_section: str | None = Field(default=None, description="来源报告章节；无对应章节时为 null。")


class QualityStructuralResponse(BaseModel):
    checks: list[dict[str, object]] = Field(description="Health 报告解析出的结构检查项。")
    findings: list[QualityFindingResponse] = Field(description="最新 Lint 任务写入的结构完整性 findings。")


class QualityResponse(BaseModel):
    snapshot: QualitySnapshotResponse = Field(description="总体状态、报告新鲜度和覆盖范围。")
    tab_counts: dict[str, int] = Field(description="各质量维度及 all 的 finding 数量。")
    structural: QualityStructuralResponse = Field(description="Health 检查项和最新 Lint 任务的结构完整性 findings。")
    consistency: dict[str, list[QualityFindingResponse]] = Field(description="最新 Lint 任务写入的语义一致性 findings，键为 findings。")
    graph: dict[str, list[QualityFindingResponse]] = Field(description="最新 Lint 任务和 Graph 报告的图谱 findings，键为 findings。")
    freshness: dict[str, list[dict[str, str]]] = Field(description="来源新鲜度建议；当前没有快照时 recommendations 为空数组。")
