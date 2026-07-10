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


settings = Settings()
