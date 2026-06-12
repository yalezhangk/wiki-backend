from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

LOGGER = logging.getLogger(__name__)
ANSWER_BLOCK_PATTERN = re.compile(r"={60}\s*(.*?)\s*={60}", re.DOTALL)
SOURCE_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")


class QueryAdapterError(RuntimeError):
    """Raised when llm-wiki-agent query invocation fails."""


@dataclass(frozen=True)
class QueryResult:
    answer: str
    sources: list[str]


class LlmWikiQueryAdapter:
    def __init__(self, repo_path: Path, python_path: Path, timeout_seconds: int) -> None:
        self._repo_path = repo_path
        self._python_path = python_path
        self._timeout_seconds = timeout_seconds

    def query(self, question: str) -> QueryResult:
        command = [
            str(self._python_path),
            "tools/query.py",
            question,
        ]
        LOGGER.info("Invoking llm-wiki-agent query")

        try:
            result = subprocess.run(
                command,
                cwd=self._repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise QueryAdapterError("llm-wiki-agent query timed out") from exc
        except OSError as exc:
            raise QueryAdapterError("failed to execute llm-wiki-agent query") from exc

        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise QueryAdapterError(f"llm-wiki-agent query failed: {stderr}")

        answer = self._extract_answer(result.stdout)
        sources = sorted(set(SOURCE_PATTERN.findall(answer)))
        return QueryResult(answer=answer, sources=sources)

    @staticmethod
    def _extract_answer(output: str) -> str:
        match = ANSWER_BLOCK_PATTERN.search(output)
        if match is None:
            raise QueryAdapterError("unable to parse llm-wiki-agent query output")
        answer = match.group(1).strip()
        if not answer:
            raise QueryAdapterError("llm-wiki-agent returned an empty answer")
        return answer


query_adapter = LlmWikiQueryAdapter(
    repo_path=Path(settings.llm_wiki_repo_path),
    python_path=Path(settings.llm_wiki_python_path),
    timeout_seconds=settings.query_timeout_seconds,
)
