from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Literal
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.config import settings
from app.llm_config import LLMProfile
from app.schemas.model_profile import ModelProfileId, ModelProfileResponse

LOGGER = logging.getLogger(__name__)
MODEL_PROFILE_AVAILABILITY_CACHE_SECONDS = 60
DEEPSEEK_MAX_TOKENS = 8192
DEEPSEEK_TEMPERATURE = 0.2
LOCAL_QWEN_MAX_TOKENS = 512
LOCAL_QWEN_TEMPERATURE = 0.2


class ModelProfileError(RuntimeError):
    """模型档案不能用于本轮回答。"""


class ModelProfileDisabledError(ModelProfileError):
    """请求的模型档案未启用。"""


class ModelProfileUnavailableError(ModelProfileError):
    """请求的模型档案暂不可用。"""


@dataclass(frozen=True)
class AnswerModelProfile:
    id: ModelProfileId
    label: str
    location: Literal["cloud", "local"]
    reasoning_mode: Literal["provider_managed", "direct", "thinking"]
    enabled: bool
    llm_profile: LLMProfile


class ModelProfileService:
    """维护受控回答模型档案及其短时可用性缓存。"""

    def __init__(
        self,
        *,
        availability_checker: Callable[[AnswerModelProfile], bool] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._profiles = self._build_profiles()
        self._availability_checker = availability_checker or self._check_availability
        self._now = now
        self._availability: dict[ModelProfileId, bool] = {
            profile_id: self._initial_availability(profile)
            for profile_id, profile in self._profiles.items()
        }
        self._refreshed_at: dict[ModelProfileId, float] = {}

    def list_profiles(self) -> list[ModelProfileResponse]:
        """返回已启用档案与缓存的可用性，不触发同步健康探测。"""
        return [
            ModelProfileResponse(
                id=profile.id,
                label=profile.label,
                location=profile.location,
                reasoning_mode=profile.reasoning_mode,
                available=self._availability[profile.id],
                is_default=profile.id == settings.model_profile_default_id,
            )
            for profile in self._profiles.values()
            if profile.enabled
        ]

    def resolve_for_turn(self, profile_id: ModelProfileId) -> AnswerModelProfile:
        """在任何聊天消息写入前解析并确认指定档案。"""
        profile = self._profiles[profile_id]
        if not profile.enabled:
            raise ModelProfileDisabledError("model profile is disabled")
        self._refresh_if_stale(profile)
        if not self._availability[profile.id]:
            raise ModelProfileUnavailableError("model profile is currently unavailable")
        return profile

    def refresh_availability(self) -> None:
        """刷新所有已启用档案的健康状态，供应用启动时调用。"""
        for profile in self._profiles.values():
            if profile.enabled:
                self._refresh(profile)

    def _refresh_if_stale(self, profile: AnswerModelProfile) -> None:
        refreshed_at = self._refreshed_at.get(profile.id)
        if refreshed_at is None or self._now() - refreshed_at >= MODEL_PROFILE_AVAILABILITY_CACHE_SECONDS:
            self._refresh(profile)

    def _refresh(self, profile: AnswerModelProfile) -> None:
        try:
            available = self._availability_checker(profile)
        except Exception:
            LOGGER.warning("Model profile health check failed profile_id=%s", profile.id, exc_info=True)
            available = False
        self._availability[profile.id] = available
        self._refreshed_at[profile.id] = self._now()

    @staticmethod
    def _initial_availability(profile: AnswerModelProfile) -> bool:
        if not profile.enabled:
            return False
        if profile.location == "cloud":
            return bool(profile.llm_profile.api_key)
        return False

    @staticmethod
    def _check_availability(profile: AnswerModelProfile) -> bool:
        if profile.location == "cloud":
            return bool(profile.llm_profile.api_key)

        api_base = profile.llm_profile.api_base
        if not api_base:
            return False
        request = Request(f"{api_base.rstrip('/')}/api/tags", method="GET")
        try:
            with urlopen(request, timeout=2.0) as response:  # noqa: S310 - 仅访问服务器受控的 Ollama 地址。
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, ValueError):
            return False
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return False
        return any(
            isinstance(model, dict) and model.get("name") == profile.llm_profile.model
            for model in models
        )

    @staticmethod
    def _build_profiles() -> dict[ModelProfileId, AnswerModelProfile]:
        return {
            "deepseek-v4-pro": AnswerModelProfile(
                id="deepseek-v4-pro",
                label="DeepSeek V4 Pro",
                location="cloud",
                reasoning_mode="provider_managed",
                enabled="deepseek-v4-pro" in settings.model_profile_enabled_ids,
                llm_profile=LLMProfile(
                    provider="deepseek",
                    model="deepseek-v4-pro",
                    api_key=settings.deepseek_api_key or settings.legacy_llm_api_key,
                    api_base=settings.deepseek_api_base,
                    max_tokens=DEEPSEEK_MAX_TOKENS,
                    temperature=DEEPSEEK_TEMPERATURE,
                ),
            ),
            "deepseek-v4-flash": AnswerModelProfile(
                id="deepseek-v4-flash",
                label="DeepSeek V4 Flash",
                location="cloud",
                reasoning_mode="provider_managed",
                enabled="deepseek-v4-flash" in settings.model_profile_enabled_ids,
                llm_profile=LLMProfile(
                    provider="deepseek",
                    model="deepseek-v4-flash",
                    api_key=settings.deepseek_api_key or settings.legacy_llm_api_key,
                    api_base=settings.deepseek_api_base,
                    max_tokens=DEEPSEEK_MAX_TOKENS,
                    temperature=DEEPSEEK_TEMPERATURE,
                ),
            ),
            "local-qwen3.6-35b-direct": AnswerModelProfile(
                id="local-qwen3.6-35b-direct",
                label="Qwen3.6 35B · 直接回答",
                location="local",
                reasoning_mode="direct",
                enabled="local-qwen3.6-35b-direct" in settings.model_profile_enabled_ids,
                llm_profile=LLMProfile(
                    provider="ollama_chat",
                    model="qwen3.6:35b",
                    api_key=None,
                    api_base=settings.ollama_api_base
                    or settings.legacy_llm_api_base
                    or "http://127.0.0.1:11434",
                    max_tokens=LOCAL_QWEN_MAX_TOKENS,
                    temperature=LOCAL_QWEN_TEMPERATURE,
                    reasoning_effort="none",
                ),
            ),
            "local-qwen3.6-35b-thinking": AnswerModelProfile(
                id="local-qwen3.6-35b-thinking",
                label="Qwen3.6 35B · 深度思考",
                location="local",
                reasoning_mode="thinking",
                enabled="local-qwen3.6-35b-thinking" in settings.model_profile_enabled_ids,
                llm_profile=LLMProfile(
                    provider="ollama_chat",
                    model="qwen3.6:35b",
                    api_key=None,
                    api_base=settings.ollama_api_base
                    or settings.legacy_llm_api_base
                    or "http://127.0.0.1:11434",
                    max_tokens=LOCAL_QWEN_MAX_TOKENS,
                    temperature=LOCAL_QWEN_TEMPERATURE,
                    reasoning_effort="low",
                ),
            ),
        }
