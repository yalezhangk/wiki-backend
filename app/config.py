from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.schemas.model_profile import ModelProfileId

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "wiki-backend"
    llm_wiki_repo_path: Path = Field(
        default=(PROJECT_ROOT.parent / "llm-wiki-agent").resolve(),
        validation_alias="WIKI_AGENT_REPO_PATH",
    )
    mysql_host: str = Field(
        default="127.0.0.1",
        validation_alias="WIKI_BACKEND_MYSQL_HOST",
    )
    mysql_port: int = Field(default=3306, validation_alias="WIKI_BACKEND_MYSQL_PORT")
    mysql_user: str = Field(default="root", validation_alias="WIKI_BACKEND_MYSQL_USER")
    mysql_password: str = Field(
        default="",
        validation_alias="WIKI_BACKEND_MYSQL_PASSWORD",
    )
    mysql_database: str = Field(
        default="wiki_backend",
        validation_alias="WIKI_BACKEND_MYSQL_DATABASE",
    )
    default_chat_title: str = Field(
        default="新对话",
        validation_alias="WIKI_BACKEND_DEFAULT_CHAT_TITLE",
    )
    chat_history_limit: int = Field(
        default=6,
        validation_alias="WIKI_BACKEND_CHAT_HISTORY_LIMIT",
    )
    ingest_max_upload_bytes: int = Field(
        default=10 * 1024 * 1024,
        gt=0,
        validation_alias="WIKI_BACKEND_INGEST_MAX_UPLOAD_BYTES",
    )
    ingest_llm_max_tokens: int = Field(
        default=8192,
        gt=0,
        validation_alias="WIKI_BACKEND_INGEST_LLM_MAX_TOKENS",
    )
    ingest_enable_marker_ocr: bool = Field(
        default=False,
        validation_alias="WIKI_BACKEND_INGEST_ENABLE_MARKER_OCR",
    )
    scheduled_ingest_root: Path | None = Field(
        default=None,
        validation_alias="WIKI_BACKEND_SCHEDULED_INGEST_ROOT",
    )
    scheduled_ingest_api_url: str = Field(
        default="http://127.0.0.1:8081",
        validation_alias="WIKI_BACKEND_SCHEDULED_INGEST_API_URL",
    )
    scheduled_ingest_poll_seconds: float = Field(
        default=2.0,
        gt=0.0,
        validation_alias="WIKI_BACKEND_SCHEDULED_INGEST_POLL_SECONDS",
    )
    scheduled_ingest_poll_timeout_seconds: int = Field(
        default=7200,
        gt=0,
        validation_alias="WIKI_BACKEND_SCHEDULED_INGEST_POLL_TIMEOUT_SECONDS",
    )
    quartz_repo_path: Path = Field(
        default=(PROJECT_ROOT.parent / "quartz").resolve(),
        validation_alias="WIKI_BACKEND_QUARTZ_REPO_PATH",
    )
    publish_node_executable: str = Field(
        default="node",
        validation_alias="WIKI_BACKEND_PUBLISH_NODE_EXECUTABLE",
    )
    publish_build_timeout_seconds: int = Field(
        default=900,
        gt=0,
        validation_alias="WIKI_BACKEND_PUBLISH_BUILD_TIMEOUT_SECONDS",
    )
    publish_debounce_seconds: int = Field(
        default=120,
        ge=0,
        validation_alias="WIKI_BACKEND_PUBLISH_DEBOUNCE_SECONDS",
    )
    publish_max_delay_seconds: int = Field(
        default=600,
        gt=0,
        validation_alias="WIKI_BACKEND_PUBLISH_MAX_DELAY_SECONDS",
    )
    quality_stale_after_hours: int = Field(
        default=168,
        gt=0,
        validation_alias="WIKI_BACKEND_QUALITY_STALE_AFTER_HOURS",
    )
    llm_provider: str = Field(
        default="deepseek",
        validation_alias="WIKI_BACKEND_LLM_PROVIDER",
    )
    llm_fast_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias="WIKI_BACKEND_LLM_FAST_MODEL",
    )
    llm_main_model: str = Field(
        default="deepseek-v4-pro",
        validation_alias="WIKI_BACKEND_LLM_MAIN_MODEL",
    )
    legacy_llm_api_key: str | None = Field(
        default=None,
        validation_alias="WIKI_BACKEND_LLM_API_KEY",
    )
    legacy_llm_api_base: str | None = Field(
        default=None,
        validation_alias="WIKI_BACKEND_LLM_API_BASE",
    )
    llm_fast_max_tokens: int = Field(
        default=1024,
        gt=0,
        validation_alias="WIKI_BACKEND_LLM_FAST_MAX_TOKENS",
    )
    llm_main_max_tokens: int = Field(
        default=4096,
        gt=0,
        validation_alias="WIKI_BACKEND_LLM_MAIN_MAX_TOKENS",
    )
    llm_fast_temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        validation_alias="WIKI_BACKEND_LLM_FAST_TEMPERATURE",
    )
    llm_main_temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        validation_alias="WIKI_BACKEND_LLM_MAIN_TEMPERATURE",
    )
    deepseek_api_key: str | None = Field(
        default=None,
        validation_alias="WIKI_BACKEND_DEEPSEEK_API_KEY",
    )
    deepseek_api_base: str = Field(
        default="https://api.deepseek.com",
        validation_alias="WIKI_BACKEND_DEEPSEEK_API_BASE",
    )
    ollama_api_base: str | None = Field(
        default=None,
        validation_alias="WIKI_BACKEND_OLLAMA_API_BASE",
    )
    model_profile_default_id: ModelProfileId = Field(
        default="deepseek-v4-flash",
        validation_alias="WIKI_BACKEND_MODEL_PROFILE_DEFAULT_ID",
    )
    model_profile_enabled_ids: Annotated[tuple[ModelProfileId, ...], NoDecode] = Field(
        default=(
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "local-qwen3.6-35b-direct",
            "local-qwen3.6-35b-thinking",
        ),
        validation_alias="WIKI_BACKEND_MODEL_PROFILE_ENABLED_IDS",
    )

    @field_validator("model_profile_enabled_ids", mode="before")
    @classmethod
    def parse_model_profile_enabled_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("model_profile_enabled_ids")
    @classmethod
    def validate_unique_model_profile_ids(
        cls,
        value: tuple[ModelProfileId, ...],
    ) -> tuple[ModelProfileId, ...]:
        if not value:
            raise ValueError("WIKI_BACKEND_MODEL_PROFILE_ENABLED_IDS cannot be empty")
        if len(set(value)) != len(value):
            raise ValueError("WIKI_BACKEND_MODEL_PROFILE_ENABLED_IDS cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_default_model_profile_is_enabled(self) -> Settings:
        if self.model_profile_default_id not in self.model_profile_enabled_ids:
            raise ValueError("WIKI_BACKEND_MODEL_PROFILE_DEFAULT_ID must be enabled")
        return self


settings = Settings()
