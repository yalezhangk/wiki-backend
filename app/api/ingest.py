from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Request, UploadFile, status

from app.schemas.ingest import IngestJobResponse
from app.services.ingest_service import (
    IngestConflictError,
    IngestNotFoundError,
    IngestService,
    IngestServiceError,
    IngestValidationError,
)
from app.storage.mysql import StorageError, StorageUnavailableError

router = APIRouter(
    prefix="/api/ingest/jobs",
    tags=["ingest"],
)


def get_ingest_service(request: Request) -> IngestService:
    return request.app.state.ingest_service


@router.post(
    "",
    response_model=IngestJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建 Ingest 导入任务",
    description=(
        "上传一个知识源文件，并创建异步 Ingest 任务。"
        "\n\n"
        "调用方式："
        "\n"
        "- 使用 `multipart/form-data` 传入 `file`"
        "\n"
        "- `auto_convert` 默认为 `true`，允许服务端先把非 Markdown 文件转换为 Markdown"
        "\n"
        "- 服务端按 `WIKI_BACKEND_INGEST_MAX_UPLOAD_BYTES` 限制大小，并校验声明类型和关键文件签名"
        "\n"
        "- 返回 `202 Accepted` 表示任务已入队，不表示 Wiki 写入已经完成"
    ),
)
async def create_ingest_job(
    file: UploadFile = File(...),
    auto_convert: bool = Form(default=True),
    ingest_service: IngestService = Depends(get_ingest_service),
) -> IngestJobResponse:
    """保存上传文件，创建 Ingest 任务，并返回初始任务状态。"""
    try:
        # Ingest 写入 Wiki 的过程由后台 worker 执行，这里只负责创建并入队任务。
        return await ingest_service.create_job(file=file, auto_convert=auto_convert)
    except IngestValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IngestConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "",
    response_model=list[IngestJobResponse],
    summary="获取 Ingest 任务列表",
    description=(
        "按创建时间倒序返回最近的 Ingest 任务，通常用于上传侧边栏或任务历史列表。"
        "\n\n"
        "每个任务包含真实 `stage`、离散 `progress_percent` 和 `updated_at`；这些字段不表示 Quartz 发布状态。"
        "\n\n"
        "`limit` 用于限制返回数量，服务端会把它约束在 1 到 100 之间。"
    ),
)
def list_ingest_jobs(
    limit: int = 20,
    ingest_service: IngestService = Depends(get_ingest_service),
) -> list[IngestJobResponse]:
    """列出最近的 Ingest 任务及其当前处理状态。"""
    try:
        return ingest_service.list_jobs(limit)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/{job_id}",
    response_model=IngestJobResponse,
    summary="获取单个 Ingest 任务详情",
    description=(
        "根据任务 ID 查询 Ingest 任务详情。"
        "\n\n"
        "响应会包含任务状态、真实处理阶段、离散进度、上传源路径、已创建或更新的 Wiki 页面、"
        "冲突记录、校验结果和失败原因。失败任务会保留失败前最后一个阶段。"
    ),
)
def get_ingest_job(
    job_id: Annotated[int, Path(gt=0)],
    ingest_service: IngestService = Depends(get_ingest_service),
) -> IngestJobResponse:
    """查询指定 Ingest 任务的完整详情。"""
    try:
        return ingest_service.get_job(job_id)
    except IngestNotFoundError as exc:
        raise HTTPException(status_code=404, detail="ingest job not found") from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except IngestServiceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
