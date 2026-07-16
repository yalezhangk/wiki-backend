from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    llm_fast_provider: str = Field(
        default="deepseek",
        validation_alias="WIKI_BACKEND_LLM_FAST_PROVIDER",
    )
    llm_fast_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias="WIKI_BACKEND_LLM_FAST_MODEL",
    )
    llm_main_provider: str = Field(
        default="deepseek",
        validation_alias="WIKI_BACKEND_LLM_MAIN_PROVIDER",
    )
    llm_main_model: str = Field(
        default="deepseek-v4-pro",
        validation_alias="WIKI_BACKEND_LLM_MAIN_MODEL",
    )
    llm_api_key: str | None = Field(
        default=None,
        validation_alias="WIKI_BACKEND_LLM_API_KEY",
    )
    llm_api_base: str | None = Field(
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


settings = Settings()
