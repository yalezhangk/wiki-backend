from __future__ import annotations

from fastapi import HTTPException, Request

from app.services.publish_service import PublishService


def get_publish_service(request: Request) -> PublishService:
    service = request.app.state.publish_service
    if service is None:
        raise HTTPException(status_code=503, detail="publish service is unavailable")
    return service
