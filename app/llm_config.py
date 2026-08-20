from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from litellm import completion

from app.config import settings

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMProfile:
    """一次 LLM 调用使用的服务器受控连接配置。"""

    provider: str
    model: str
    api_key: str | None
    api_base: str | None
    max_tokens: int
    temperature: float
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class IngestModelCapabilities:
    """Ingest 模型已确认的本地 prompt 预算；None 表示由供应商管理。"""

    max_input_tokens: int | None = None
    context_window_tokens: int | None = None
    context_safety_margin_tokens: int | None = None


@dataclass(frozen=True)
class IngestModelProfile:
    """一次 Ingest 任务可使用的服务器受控模型与预算能力。"""

    model_identifier: str
    llm_profile: LLMProfile
    capabilities: IngestModelCapabilities


class LLMConfigError(RuntimeError):
    """Raised when the backend LLM client cannot return usable text."""


class LLMResponseTruncatedError(LLMConfigError):
    """Raised when the provider explicitly reports an output-length cutoff."""

    def __init__(
        self,
        *,
        model: str,
        max_tokens: int,
        finish_reason: str,
        response_content: str | None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.finish_reason = finish_reason
        self.response_content = response_content
        super().__init__("LLM response was truncated by the provider")


def _resolve_model(provider: str, model: str) -> str:
    normalized_provider = provider.strip()
    normalized_model = model.strip()
    if not normalized_model:
        raise LLMConfigError("LLM model cannot be empty")
    if normalized_provider and "/" not in normalized_model:
        return f"{normalized_provider}/{normalized_model}"
    return normalized_model


def _is_ollama_provider(provider: str) -> bool:
    return provider.strip().lower() in {"ollama", "ollama_chat"}


def _resolve_api_key(provider: str) -> str | None:
    normalized_provider = provider.strip().lower()
    if _is_ollama_provider(normalized_provider):
        return None
    if normalized_provider == "deepseek":
        return settings.deepseek_api_key or settings.legacy_llm_api_key
    return settings.legacy_llm_api_key


def _resolve_api_base(provider: str) -> str | None:
    """根据 provider 选择连接地址，旧地址只在同类 provider 下兼容。"""
    normalized_provider = provider.strip().lower()
    if _is_ollama_provider(normalized_provider):
        return settings.ollama_api_base or settings.legacy_llm_api_base or "http://127.0.0.1:11434"
    if normalized_provider == "deepseek":
        return settings.deepseek_api_base
    return settings.legacy_llm_api_base


def resolve_ingest_model_profile(model_identifier: str | None = None) -> IngestModelProfile:
    """解析 Ingest 专用白名单模型，拒绝由环境变量任意指定的提供商或模型。"""

    configured_identifier = (
        model_identifier
        or f"{settings.ingest_provider.strip()}/{settings.ingest_model.strip()}"
    )
    profiles = {
        "deepseek/deepseek-v4-pro": IngestModelCapabilities(),
        "deepseek/deepseek-v4-flash": IngestModelCapabilities(),
        "ollama_chat/qwen3.6:35b": IngestModelCapabilities(
            max_input_tokens=49152,
            context_window_tokens=65536,
            context_safety_margin_tokens=8192,
        ),
    }
    capabilities = profiles.get(configured_identifier)
    if capabilities is None:
        raise LLMConfigError(f"unsupported ingest model: {configured_identifier}")

    provider, model = configured_identifier.split("/", 1)
    reasoning_effort = settings.ingest_reasoning_effort
    if configured_identifier.startswith("deepseek/"):
        # Ingest requires one complete machine-parsed JSON object. DeepSeek's
        # default thinking can consume the full completion budget before it
        # emits any final content, so this workflow must stay direct.
        reasoning_effort = "none"
    if configured_identifier == "ollama_chat/qwen3.6:35b":
        if reasoning_effort not in {None, "", "none"}:
            raise LLMConfigError("qwen3.6:35b ingest requires reasoning_effort=none")
        reasoning_effort = "none"
    if (
        capabilities.context_window_tokens is not None
        and settings.ingest_llm_max_tokens + capabilities.context_safety_margin_tokens
        > capabilities.context_window_tokens
    ):
        raise LLMConfigError("ingest output and safety budget exceed the model context window")

    return IngestModelProfile(
        model_identifier=configured_identifier,
        llm_profile=LLMProfile(
            provider=provider,
            model=model,
            api_key=_resolve_api_key(provider),
            api_base=_resolve_api_base(provider),
            max_tokens=settings.ingest_llm_max_tokens,
            temperature=settings.llm_main_temperature,
            reasoning_effort=reasoning_effort or None,
        ),
        capabilities=capabilities,
    )


def call_llm(
    prompt: str,
    *,
    fast: bool,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """Call the backend-configured LLM through LiteLLM."""
    provider = settings.llm_provider
    if fast:
        model = settings.llm_fast_model
        default_max_tokens = settings.llm_fast_max_tokens
        default_temperature = settings.llm_fast_temperature
    else:
        model = settings.llm_main_model
        default_max_tokens = settings.llm_main_max_tokens
        default_temperature = settings.llm_main_temperature

    return call_llm_profile(
        prompt,
        LLMProfile(
            provider=provider,
            model=model,
            api_key=_resolve_api_key(provider),
            api_base=_resolve_api_base(provider),
            max_tokens=default_max_tokens,
            temperature=default_temperature,
        ),
        max_tokens=max_tokens,
        temperature=temperature,
    )


def call_llm_profile(
    prompt: str,
    profile: LLMProfile,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """Call a single server-controlled answer model profile through LiteLLM."""
    kwargs: dict[str, Any] = {
        "model": _resolve_model(profile.provider, profile.model),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens if max_tokens is not None else profile.max_tokens,
        "temperature": temperature if temperature is not None else profile.temperature,
    }
    if profile.api_key and not _is_ollama_provider(profile.provider):
        kwargs["api_key"] = profile.api_key
    if profile.api_base:
        kwargs["api_base"] = profile.api_base
    if profile.reasoning_effort is not None:
        kwargs["reasoning_effort"] = profile.reasoning_effort
    if (
        profile.provider.strip().lower() == "deepseek"
        and profile.reasoning_effort == "none"
    ):
        # DeepSeek V4 enables thinking by default. LiteLLM maps
        # reasoning_effort="none" by omitting the field, so send the
        # provider's explicit switch through its OpenAI-compatible extra body.
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    reasoning_effort = profile.reasoning_effort or "provider_default"
    started_at = time.monotonic()
    LOGGER.info(
        "LLM completion started provider=%s model=%s reasoning_effort=%s max_tokens=%s",
        profile.provider,
        profile.model,
        reasoning_effort,
        kwargs["max_tokens"],
    )
    try:
        response = completion(**kwargs)
    except Exception:
        LOGGER.exception(
            "LLM completion failed provider=%s model=%s reasoning_effort=%s elapsed_ms=%s",
            profile.provider,
            profile.model,
            reasoning_effort,
            round((time.monotonic() - started_at) * 1000),
        )
        raise
    try:
        choice = response.choices[0]
        content = choice.message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise LLMConfigError("LLM returned an invalid response structure") from exc

    finish_reason = getattr(choice, "finish_reason", None)
    reasoning_trace_present = _has_reasoning_trace(choice.message)
    LOGGER.info(
        "LLM completion completed provider=%s model=%s reasoning_effort=%s elapsed_ms=%s "
        "finish_reason=%s response_chars=%s reasoning_trace_present=%s",
        profile.provider,
        profile.model,
        reasoning_effort,
        round((time.monotonic() - started_at) * 1000),
        finish_reason or "unknown",
        len(content) if isinstance(content, str) else 0,
        reasoning_trace_present,
    )
    if isinstance(finish_reason, str) and finish_reason.lower() == "length":
        LOGGER.warning(
            "LLM response was truncated model=%s max_tokens=%s finish_reason=%s",
            kwargs["model"],
            kwargs["max_tokens"],
            finish_reason,
        )
        raise LLMResponseTruncatedError(
            model=str(kwargs["model"]),
            max_tokens=int(kwargs["max_tokens"]),
            finish_reason=finish_reason,
            response_content=content if isinstance(content, str) else None,
        )
    if not isinstance(content, str) or not content.strip():
        raise LLMConfigError("LLM returned an empty response")
    return content


def _has_reasoning_trace(message: Any) -> bool:
    """只记录是否返回了推理字段，绝不记录推理正文。"""
    return any(
        bool(getattr(message, field, None))
        for field in ("thinking", "reasoning_content")
    )


def call_llm_fast(prompt: str, max_tokens: int | None = None) -> str:
    """Use the fast model for page selection and lightweight extraction."""
    return call_llm(prompt, fast=True, max_tokens=max_tokens)


def call_llm_main(prompt: str, max_tokens: int | None = None) -> str:
    """Use the main model for answers and ingest generation."""
    return call_llm(prompt, fast=False, max_tokens=max_tokens)
