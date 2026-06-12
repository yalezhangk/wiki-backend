from __future__ import annotations

import os
from pathlib import Path

from pydantic import DirectoryPath, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "wiki-backend"
    llm_wiki_repo_path: DirectoryPath = Field(
        default=Path(os.getenv("WIKI_AGENT_REPO_PATH", "..\\llm-wiki-agent")).resolve()
    )
    db_path: Path = Field(default=Path(os.getenv("WIKI_BACKEND_DB_PATH", "data/wiki_backend.db")))


settings = Settings()
