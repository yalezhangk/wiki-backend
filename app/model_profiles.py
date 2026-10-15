from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import settings
from app.llm_config import LLMProfile
from app.schemas.model_profile import ModelProfileId, ModelProfileResponse

LOGGER = logging.getLogger(__name__)
MODEL_PROFILE_AVAILABILITY_CACHE_SECONDS = 60
OLLAMA_AVAILABILITY_TIMEOUT_SECONDS = 5.0
DEEPSEEK_MAX_TOKENS = 8192
DEEPSEEK_TEMPERATURE = 0.2
LOCAL_QWEN_DIRECT_MAX_TOKENS = 1536
LOCAL_QWEN_THINKING_MAX_TOKENS = 4096
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
        self._availability_lock = threading.RLock()

    def list_profiles(self) -> list[ModelProfileResponse]:
        """返回已启用档案，并在缓存过期时刷新可用性。"""
        profiles = tuple(profile for profile in self._profiles.values() if profile.enabled)
        with self._availability_lock:
            self._refresh_stale(profiles)
            return [
                ModelProfileResponse(
                    id=profile.id,
                    label=profile.label,
                    location=profile.location,
                    reasoning_mode=profile.reasoning_mode,
                    available=self._availability[profile.id],
                    is_default=profile.id == settings.model_profile_default_id,
                )
                for profile in profiles
            ]

    def resolve_for_turn(self, profile_id: ModelProfileId) -> AnswerModelProfile:
        """在任何聊天消息写入前解析并确认指定档案。"""
        profile = self._profiles[profile_id]
        if not profile.enabled:
            raise ModelProfileDisabledError("model profile is disabled")
        with self._availability_lock:
            matching_profiles = self._matching_enabled_profiles(profile)
            self._refresh_stale(matching_profiles)
            if not self._availability[profile.id]:
                raise ModelProfileUnavailableError("model profile is currently unavailable")
        return profile

    def refresh_availability(self) -> None:
        """刷新所有已启用档案的健康状态，供应用启动时调用。"""
        profiles = tuple(profile for profile in self._profiles.values() if profile.enabled)
        with self._availability_lock:
            self._refresh(profiles)

    def _refresh_stale(self, profiles: tuple[AnswerModelProfile, ...]) -> None:
        now = self._now()
        stale_profiles = tuple(
            profile
            for profile in profiles
            if (refreshed_at := self._refreshed_at.get(profile.id)) is None
            or now - refreshed_at >= MODEL_PROFILE_AVAILABILITY_CACHE_SECONDS
        )
        if stale_profiles:
            self._refresh(stale_profiles)

    def _refresh(self, profiles: tuple[AnswerModelProfile, ...]) -> None:
        profiles_by_target: dict[tuple[str, str | None, str], list[AnswerModelProfile]] = {}
        for profile in profiles:
            target = self._availability_target(profile)
            profiles_by_target.setdefault(target, []).append(profile)

        for target_profiles in profiles_by_target.values():
            checked_profile = target_profiles[0]
            try:
                available = self._availability_checker(checked_profile)
            except Exception:
                LOGGER.warning(
                    "Model profile health check failed profile_id=%s target=%s",
                    checked_profile.id,
                    self._availability_target(checked_profile),
                    exc_info=True,
                )
                available = False
            refreshed_at = self._now()
            for profile in target_profiles:
                self._availability[profile.id] = available
                self._refreshed_at[profile.id] = refreshed_at

    def _matching_enabled_profiles(
        self,
        profile: AnswerModelProfile,
    ) -> tuple[AnswerModelProfile, ...]:
        target = self._availability_target(profile)
        return tuple(
            candidate
            for candidate in self._profiles.values()
            if candidate.enabled and self._availability_target(candidate) == target
        )

    @staticmethod
    def _availability_target(profile: AnswerModelProfile) -> tuple[str, str | None, str]:
        return (
            profile.llm_profile.provider,
            profile.llm_profile.api_base,
            profile.llm_profile.model,
        )

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
        target_url = f"{api_base.rstrip('/')}/api/tags"
        request = Request(target_url, method="GET")
        try:
            with urlopen(  # noqa: S310 - 仅访问服务器受控的 Ollama 地址。
                request,
                timeout=OLLAMA_AVAILABILITY_TIMEOUT_SECONDS,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            LOGGER.warning(
                "Ollama model health check returned HTTP error url=%s status=%s reason=%s",
                target_url,
                exc.code,
                exc.reason,
            )
            return False
        except (OSError, URLError) as exc:
            LOGGER.warning(
                "Ollama model health check request failed url=%s error=%s",
                target_url,
                exc,
            )
            return False
        except ValueError as exc:
            LOGGER.warning(
                "Ollama model health check returned invalid JSON url=%s error=%s",
                target_url,
                exc,
            )
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
            "local-qwen3.8-27b-direct": AnswerModelProfile(
                id="local-qwen3.8-27b-direct",
                label="Qwen3.8 27B · 直接回答",
                location="local",
                reasoning_mode="direct",
                enabled="local-qwen3.8-27b-direct" in settings.model_profile_enabled_ids,
                llm_profile=LLMProfile(
                    provider="ollama_chat",
                    model="qwen3.8:27b",
                    api_key=None,
                    api_base=settings.ollama_api_base
                    or settings.legacy_llm_api_base
                    or "http://127.0.0.1:11434",
                    max_tokens=LOCAL_QWEN_DIRECT_MAX_TOKENS,
                    temperature=LOCAL_QWEN_TEMPERATURE,
                    reasoning_effort="none",
                ),
            ),
            "local-qwen3.8-27b-thinking": AnswerModelProfile(
                id="local-qwen3.8-27b-thinking",
                label="Qwen3.8 27B · 深度思考",
                location="local",
                reasoning_mode="thinking",
                enabled="local-qwen3.8-27b-thinking" in settings.model_profile_enabled_ids,
                llm_profile=LLMProfile(
                    provider="ollama_chat",
                    model="qwen3.8:27b",
                    api_key=None,
                    api_base=settings.ollama_api_base
                    or settings.legacy_llm_api_base
                    or "http://127.0.0.1:11434",
                    max_tokens=LOCAL_QWEN_THINKING_MAX_TOKENS,
                    temperature=LOCAL_QWEN_TEMPERATURE,
                    reasoning_effort="low",
                ),
            ),
        }
