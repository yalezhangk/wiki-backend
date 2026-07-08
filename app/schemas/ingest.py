from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

IngestJobStatus = Literal["queued", "running", "succeeded", "failed"]


class IngestValidation(BaseModel):
    broken_links: list[tuple[str, str]] = Field(default_factory=list)
    unindexed: list[str] = Field(default_factory=list)


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
    job_id: str
    status: IngestJobStatus
    original_filename: str
    source_path: str
    created_pages: list[str] = Field(default_factory=list)
    updated_pages: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    validation: IngestValidation = Field(default_factory=IngestValidation)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
