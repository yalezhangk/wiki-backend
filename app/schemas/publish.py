from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


PublicationStatus = Literal["pending", "running", "published", "failed"]
PublishJobStatus = Literal["queued", "running", "succeeded", "failed"]
PublishTrigger = Literal["automatic", "manual"]


class PublicationResponse(BaseModel):
    """某项 Wiki 写入对应的站点发布状态。"""

    status: PublicationStatus
    job_id: int | None = Field(default=None, gt=0)
    published_at: datetime | None = None
    error: str | None = None


class PublishJobResponse(BaseModel):
    job_id: int = Field(gt=0)
    status: PublishJobStatus
    trigger: PublishTrigger
    change_count: int = Field(ge=0)
    scheduled_at: datetime
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    published_at: datetime | None = None
    error: str | None = None


class PublishStatusResponse(BaseModel):
    pending_change_count: int = Field(ge=0)
    active_job: PublishJobResponse | None = None
    last_successful_job: PublishJobResponse | None = None
