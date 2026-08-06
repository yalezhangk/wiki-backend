from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.config import settings
from app.model_profiles import ModelProfileService
from app.schemas.model_profile import (
    InternalModelResponse,
    ModelOverviewResponse,
    ModelProfileResponse,
)

router = APIRouter(prefix="/api/model-profiles", tags=["model-profiles"])


def get_model_profile_service(request: Request) -> ModelProfileService:
    return request.app.state.model_profile_service


@router.get(
    "",
    response_model=list[ModelProfileResponse],
    summary="获取可用回答模型档案",
    description="只返回浏览器可见的档案 ID、显示名称、执行位置、推理策略和缓存的可用状态。",
)
def list_model_profiles(
    model_profile_service: ModelProfileService = Depends(get_model_profile_service),
) -> list[ModelProfileResponse]:
    """列出已启用的受控回答模型档案。"""
    return model_profile_service.list_profiles()


@router.get(
    "/overview",
    response_model=ModelOverviewResponse,
    summary="获取系统设置模型概览",
    description=(
        "返回 Chat 已启用档案，以及当前 FAST、MAIN 内部任务的 provider 和模型名；"
        "不返回凭据、模型服务地址或未启用档案。"
    ),
)
def get_model_overview(
    model_profile_service: ModelProfileService = Depends(get_model_profile_service),
) -> ModelOverviewResponse:
    """返回由当前服务端配置决定的只读模型概览。"""
    return ModelOverviewResponse(
        chat_models=model_profile_service.list_profiles(),
        fast_model=InternalModelResponse(
            provider=settings.llm_provider,
            model=settings.llm_fast_model,
        ),
        main_model=InternalModelResponse(
            provider=settings.llm_provider,
            model=settings.llm_main_model,
        ),
    )
