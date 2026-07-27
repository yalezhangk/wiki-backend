from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.main_dependencies import get_publish_service
from app.schemas.publish import PublishJobResponse, PublishStatusResponse
from app.services.publish_service import PublishNotFoundError, PublishService


router = APIRouter(prefix="/api/publish", tags=["publish"])


@router.get("/status", response_model=PublishStatusResponse, summary="查询 Quartz 发布状态")
def get_publish_status(
    publish_service: PublishService = Depends(get_publish_service),
) -> PublishStatusResponse:
    return publish_service.get_status()


@router.post("/jobs", response_model=PublishJobResponse, status_code=202, summary="立即构建并发布 Quartz")
def create_publish_job(
    publish_service: PublishService = Depends(get_publish_service),
) -> PublishJobResponse:
    return publish_service.request_manual_publish()


@router.get("/jobs", response_model=list[PublishJobResponse], summary="查询最近发布任务")
def list_publish_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    publish_service: PublishService = Depends(get_publish_service),
) -> list[PublishJobResponse]:
    return publish_service.list_jobs(limit)


@router.get("/jobs/{job_id}", response_model=PublishJobResponse, summary="查询发布任务详情")
def get_publish_job(
    job_id: str,
    publish_service: PublishService = Depends(get_publish_service),
) -> PublishJobResponse:
    try:
        return publish_service.get_job(job_id)
    except PublishNotFoundError as exc:
        raise HTTPException(status_code=404, detail="publish job not found") from exc
