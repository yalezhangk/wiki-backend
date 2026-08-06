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
