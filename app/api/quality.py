from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.main_dependencies import get_quality_report_service
from app.schemas.quality import QualityResponse
from app.services.quality_report_service import QualityReportService

router = APIRouter(prefix="/api/quality", tags=["quality"])


@router.get(
    "/latest",
    response_model=QualityResponse,
    summary="读取最近质量巡检快照",
    description=(
        "只读聚合最近的 `health-report.md`、`lint-report.md` 和 `graph-report.md`，"
        "并结合最近 maintenance 任务标记可选 LLM 阶段是否不完整。\n\n"
        "报告仅作为质量快照的定向输入，不会回流为 Health、Graph、Lint 或问答的知识页。\n\n"
        "该接口不会运行巡检、调用 LLM、写入 Wiki、创建任务或触发 Quartz 发布。"
        "报告缺失、过期或无法解析时仍返回 `200`，在 `snapshot.checks` 中给出对应状态；"
        "只有 Wiki 根目录不可访问时返回 `503`。"
    ),
    response_description="最近可用质量快照；报告缺失或过期时各检查项会明确标记状态。",
    responses={503: {"description": "Wiki 根目录不可访问，无法读取质量报告。"}},
)
def get_latest_quality(service: QualityReportService = Depends(get_quality_report_service)) -> QualityResponse:
    """只读最近报告；不会运行巡检、调用 LLM、写入 Wiki 或触发发布。"""
    try:
        return service.get_latest()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="quality report service is unavailable") from exc
