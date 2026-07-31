from __future__ import annotations

from fastapi import HTTPException, Request

from app.services.publish_service import PublishService
from app.services.maintenance_service import MaintenanceService
from app.services.quality_report_service import QualityReportService


def get_publish_service(request: Request) -> PublishService:
    service = request.app.state.publish_service
    if service is None:
        raise HTTPException(status_code=503, detail="publish service is unavailable")
    return service


def get_maintenance_service(request: Request) -> MaintenanceService:
    service = request.app.state.maintenance_service
    if service is None:
        raise HTTPException(status_code=503, detail="maintenance service is unavailable")
    return service


def get_quality_report_service(request: Request) -> QualityReportService:
    service = request.app.state.quality_report_service
    if service is None:
        raise HTTPException(status_code=503, detail="quality report service is unavailable")
    return service
