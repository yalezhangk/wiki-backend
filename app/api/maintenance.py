from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.main_dependencies import get_maintenance_service
from app.schemas.maintenance import (
    MaintenanceJobCreateRequest,
    MaintenanceJobResponse,
    MaintenanceTaskKind,
    MaintenanceWorkflowCreateRequest,
    MaintenanceWorkflowResponse,
)
from app.services.maintenance_service import MaintenanceNotFoundError, MaintenanceService

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


@router.post(
    "/jobs",
    response_model=MaintenanceJobResponse,
    status_code=202,
    summary="创建知识库维护任务",
    description=(
        "创建单个异步维护任务，并立即返回任务审计记录。`202 Accepted` 只表示已入队；"
        "请轮询 `GET /api/maintenance/jobs/{job_id}` 直到 `status` 为 `succeeded` 或 `failed`。\n\n"
        "任务与选项：\n"
        "- `health`：检查 Wiki 页面、索引和日志覆盖；`save_report` 默认 `true`，会写入 `wiki/health-report.md`。"
        '若仅验证逻辑可提交 `{"task_kind":"health","options":{"save_report":false}}`，但仍会创建 MySQL 审计任务。\n'
        "- `graph`：生成 WikiLink 图谱；`infer_relations` 默认 `true`，会调用 LLM 推断关系并可能产生费用；"
        "`save_report` 默认 `true`，控制是否写入 `graph/graph-report.md`。\n"
        "- `lint`：执行结构与可选语义巡检；`semantic_analysis` 默认 `true`，可能调用 LLM 并产生费用；"
        "`semantic_mode=agent_compat` 使用 Agent 的前 20 页 Markdown 兼容路径；"
        "`delta`、`risk`、`full`、`selected` 为后端扩展模式，不承诺 Agent 产物一致性。\n\n"
        "`semantic_mode=selected` 必须同时传 `selected_page_paths`：最多 20 个 Wiki 根目录相对 `.md` 路径，"
        "例如 `entities/example.md`。\n\n"
        "除 `health` 且 `save_report=false` 外，maintenance 任务会写入共享 Wiki 或 graph artifact；"
        "所有任务都会写入 MySQL 审计记录。"
        "创建后请轮询任务，而非把 `202` 当作巡检完成。\n\n"
        "仅允许传入当前任务类型支持的 `options` 键；未知键或不合法的 `semantic_mode` 返回 `422`。"
    ),
    responses={
        422: {"description": "请求体或 options 校验失败。"},
        503: {"description": "MySQL 未就绪，维护服务未初始化。"},
    },
)
def create_maintenance_job(
    payload: MaintenanceJobCreateRequest,
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MaintenanceJobResponse:
    return service.create_job(task_kind=payload.task_kind, options=payload.options)


@router.get(
    "/jobs",
    response_model=list[MaintenanceJobResponse],
    summary="查询维护任务",
    description=(
        "按创建时间倒序返回任务审计记录。可按任务类型或同一质量工作流的 `workflow_id` 筛选。"
        "返回中 `result_state=partial` 表示确定性检查已完成，但可选 LLM 阶段不可用；"
        "此时不能将语义或推断结论视为完整结果。"
    ),
    response_description="符合筛选条件的任务列表；空列表表示暂无匹配任务。",
)
def list_maintenance_jobs(
    limit: int = Query(default=20, ge=1, le=100, description="最大返回条数，范围为 1 到 100。"),
    task_kind: MaintenanceTaskKind | None = Query(default=None, description="可选：仅返回 health、graph 或 lint 任务。"),
    workflow_id: UUID | None = Query(default=None, description="可选：仅返回指定 quality 工作流创建的三项任务。"),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> list[MaintenanceJobResponse]:
    return service.list_jobs(limit=limit, task_kind=task_kind, workflow_id=workflow_id)


@router.get(
    "/jobs/{job_id}",
    response_model=MaintenanceJobResponse,
    summary="查询维护任务详情",
    description=(
        "读取单个任务的实时审计状态、进度、结构化结果摘要和安全截断后的错误信息。"
        "`queued`、`running` 表示尚未完成；`succeeded` 或 `failed` 为终态。"
    ),
    responses={404: {"description": "任务不存在。"}, 503: {"description": "维护服务未初始化。"}},
)
def get_maintenance_job(
    job_id: Annotated[int, Path(gt=0, description="创建任务时返回的正整数 job_id。")],
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MaintenanceJobResponse:
    try:
        return service.get_job(job_id)
    except MaintenanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="maintenance job not found") from exc


@router.post(
    "/workflows/quality",
    response_model=MaintenanceWorkflowResponse,
    status_code=202,
    summary="创建质量巡检工作流",
    description=(
        "一次创建固定的 `health → graph → lint` 依赖工作流，并返回三个已入队任务。"
        "Graph 仅在 Health 成功后运行，Lint 仅在 Graph 成功后运行；前置任务失败时，后续任务不会执行。\n\n"
        "请求中的 `lint_options` 与单独创建 lint 任务时的 `options` 规则一致。"
        "工作流默认不启用 graph LLM 推断；Lint 默认启用语义分析，可能调用 LLM。"
    ),
    responses={
        422: {"description": "lint_options 校验失败。"},
        503: {"description": "MySQL 未就绪，维护服务未初始化。"},
    },
)
def create_quality_workflow(
    payload: MaintenanceWorkflowCreateRequest,
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MaintenanceWorkflowResponse:
    workflow_id, jobs = service.create_quality_workflow(lint_options=payload.lint_options)
    return MaintenanceWorkflowResponse(workflow_id=workflow_id, jobs=jobs)
