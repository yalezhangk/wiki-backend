from __future__ import annotations

from typing import Any

from litellm import completion

from app.config import settings


class LLMConfigError(RuntimeError):
    """Raised when the backend LLM client cannot return usable text."""


def _resolve_model(provider: str, model: str) -> str:
    normalized_provider = provider.strip()
    normalized_model = model.strip()
    if not normalized_model:
        raise LLMConfigError("LLM model cannot be empty")
    if normalized_provider and "/" not in normalized_model:
        return f"{normalized_provider}/{normalized_model}"
    return normalized_model


def call_llm(
    prompt: str,
    *,
    fast: bool,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """Call the backend-configured LLM through LiteLLM."""
    if fast:
        provider = settings.llm_fast_provider
        model = settings.llm_fast_model
        default_max_tokens = settings.llm_fast_max_tokens
        default_temperature = settings.llm_fast_temperature
    else:
        provider = settings.llm_main_provider
        model = settings.llm_main_model
        default_max_tokens = settings.llm_main_max_tokens
        default_temperature = settings.llm_main_temperature

    kwargs: dict[str, Any] = {
        "model": _resolve_model(provider, model),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens if max_tokens is not None else default_max_tokens,
        "temperature": temperature if temperature is not None else default_temperature,
    }
    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base

    response = completion(**kwargs)
    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise LLMConfigError("LLM returned an empty response")
    return content


def call_llm_fast(prompt: str, max_tokens: int | None = None) -> str:
    """Use the fast model for page selection and lightweight extraction."""
    return call_llm(prompt, fast=True, max_tokens=max_tokens)


def call_llm_main(prompt: str, max_tokens: int | None = None) -> str:
    """Use the main model for answers and ingest generation."""
    return call_llm(prompt, fast=False, max_tokens=max_tokens)
