from __future__ import annotations

import importlib
import json
import logging
import re
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from queue import Queue
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Protocol
from uuid import uuid4

from fastapi import UploadFile

from app.schemas.ingest import IngestJobResponse, IngestLLMResult, IngestValidation

LOGGER = logging.getLogger(__name__)

CONVERTIBLE_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".xls",
    ".html",
    ".htm",
    ".txt",
    ".csv",
    ".json",
    ".xml",
    ".rst",
    ".rtf",
    ".epub",
    ".ipynb",
    ".yaml",
    ".yml",
    ".tsv",
    ".wav",
    ".mp3",
}
ALL_SUPPORTED_EXTENSIONS = {".md"} | CONVERTIBLE_EXTENSIONS
MAX_INDEX_CONTEXT_CHARS = 16000
MAX_OVERVIEW_CONTEXT_CHARS = 10000
MAX_RECENT_SOURCE_CONTEXT_CHARS = 6000


class IngestServiceError(RuntimeError):
    """Raised when an ingest request cannot be accepted."""


class IngestValidationError(IngestServiceError):
    """Raised when an uploaded file is invalid."""


class IngestConflictError(IngestServiceError):
    """Raised when the target upload path already exists."""


class IngestNotFoundError(IngestServiceError):
    """Raised when an ingest job cannot be found."""


class IngestStorage(Protocol):
    def create_ingest_job(
        self,
        *,
        job_id: str,
        status: str,
        original_filename: str,
        stored_filename: str,
        source_path: str,
        created_at: datetime,
    ) -> IngestJobResponse:
        ...

    def get_ingest_job(self, job_id: str) -> IngestJobResponse | None:
        ...

    def list_ingest_jobs(self, limit: int) -> list[IngestJobResponse]:
        ...

    def mark_ingest_job_running(self, job_id: str, started_at: datetime) -> None:
        ...

    def mark_ingest_job_succeeded(
        self,
        *,
        job_id: str,
        created_pages: list[str],
        updated_pages: list[str],
        contradictions: list[str],
        validation: IngestValidation,
        finished_at: datetime,
    ) -> None:
        ...

    def mark_ingest_job_failed(self, *, job_id: str, error: str, finished_at: datetime) -> None:
        ...


class IngestService:
    def __init__(self, *, storage: IngestStorage, agent_root: Path, start_worker: bool = True) -> None:
        self._storage = storage
        self._agent_root = agent_root.resolve()
        self._wiki_dir = self._agent_root / "wiki"
        self._upload_dir = self._agent_root / "raw" / "uploads"
        self._schema_file = self._agent_root / "CLAUDE.md"
        self._index_file = self._wiki_dir / "index.md"
        self._overview_file = self._wiki_dir / "overview.md"
        self._log_file = self._wiki_dir / "log.md"
        self._call_llm_main: Callable[[str, int | None], str] | None = None
        self._queue: Queue[str] = Queue()
        self._worker: threading.Thread | None = None
        if start_worker:
            self._worker = threading.Thread(target=self._worker_loop, name="ingest-worker", daemon=True)
            self._worker.start()

    async def create_job(self, *, file: UploadFile, auto_convert: bool = True) -> IngestJobResponse:
        original_filename = Path(file.filename or "").name
        if not original_filename:
            raise IngestValidationError("filename cannot be empty")

        suffix = Path(original_filename).suffix.lower()
        if suffix not in ALL_SUPPORTED_EXTENSIONS:
            raise IngestValidationError(f"unsupported file extension: {suffix or '(none)'}")
        if suffix != ".md" and not auto_convert:
            raise IngestValidationError(f"non-markdown file requires auto_convert: {suffix}")

        content = await file.read()
        if not content:
            raise IngestValidationError("file cannot be empty")

        stored_filename = f"{self._utc_now().strftime('%Y%m%d-%H%M%S')}-{self._safe_filename(original_filename)}"
        relative_source_path = Path("raw") / "uploads" / stored_filename
        target_path = self._agent_root / relative_source_path
        if target_path.exists():
            raise IngestConflictError(f"upload already exists: {relative_source_path.as_posix()}")

        self._upload_dir.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(content)

        created_at = self._utc_now()
        job = self._storage.create_ingest_job(
            job_id=str(uuid4()),
            status="queued",
            original_filename=original_filename,
            stored_filename=stored_filename,
            source_path=relative_source_path.as_posix(),
            created_at=created_at,
        )
        self._queue.put(job.job_id)
        return job

    def get_job(self, job_id: str) -> IngestJobResponse:
        job = self._storage.get_ingest_job(job_id)
        if job is None:
            raise IngestNotFoundError(f"ingest job not found: {job_id}")
        return job

    def list_jobs(self, limit: int) -> list[IngestJobResponse]:
        bounded_limit = min(max(limit, 1), 100)
        return self._storage.list_ingest_jobs(bounded_limit)

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run_job(job_id)
            except Exception:
                LOGGER.exception("Unhandled ingest worker error job_id=%s", job_id)
            finally:
                self._queue.task_done()

    def _run_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        started_at = self._utc_now()
        self._storage.mark_ingest_job_running(job_id, started_at)
        try:
            result = self._ingest_source(self._agent_root / job.source_path, job_id=job_id)
            self._storage.mark_ingest_job_succeeded(
                job_id=job_id,
                created_pages=result["created_pages"],
                updated_pages=result["updated_pages"],
                contradictions=result["contradictions"],
                validation=result["validation"],
                finished_at=self._utc_now(),
            )
        except Exception as exc:
            LOGGER.exception("Ingest job failed job_id=%s", job_id)
            self._storage.mark_ingest_job_failed(
                job_id=job_id,
                error=str(exc),
                finished_at=self._utc_now(),
            )

    def _ingest_source(self, source_path: Path, *, job_id: str) -> dict[str, Any]:
        source = self._convert_to_markdown(source_path) if source_path.suffix.lower() != ".md" else source_path
        source_content = source.read_text(encoding="utf-8")
        prompt = self._build_prompt(source=source, source_content=source_content)
        raw = self._call_llm_with_retry(prompt)
        data = IngestLLMResult.model_validate(
            self._parse_llm_result_with_repair(
                prompt=prompt,
                raw=raw,
                source_path=source_path,
                job_id=job_id,
            )
        )

        created_pages = self._write_ingest_result(data)
        updated_pages = ["index.md", "log.md"]
        if data.overview_update:
            updated_pages.append("overview.md")
        validation = self._validate_ingest(created_pages)

        return {
            "created_pages": created_pages,
            "updated_pages": updated_pages,
            "contradictions": data.contradictions,
            "validation": validation,
        }

    def _parse_llm_result_with_repair(
        self,
        *,
        prompt: str,
        raw: str,
        source_path: Path,
        job_id: str,
    ) -> dict[str, Any]:
        try:
            return self._parse_json_from_response(raw)
        except IngestServiceError as first_error:
            first_debug_path = self._write_llm_debug_response(
                source_path=source_path,
                job_id=job_id,
                label="initial",
                content=raw,
            )
            LOGGER.warning(
                "LLM ingest response was not valid JSON; retrying with stricter instructions. "
                "job_id=%s debug_path=%s",
                job_id,
                first_debug_path,
            )

        repair_prompt = (
            f"{prompt}\n\n"
            "The previous response could not be parsed as a JSON object. "
            "Return the complete ingest result again as raw JSON only. "
            "Do not include markdown fences, explanations, apologies, analysis, or any text outside the JSON object."
        )
        retry_raw = self._call_llm_with_retry(repair_prompt)
        try:
            return self._parse_json_from_response(retry_raw)
        except IngestServiceError as retry_error:
            retry_debug_path = self._write_llm_debug_response(
                source_path=source_path,
                job_id=job_id,
                label="retry",
                content=retry_raw,
            )
            raise IngestServiceError(
                "LLM response did not contain a valid ingest JSON object. "
                f"Debug responses saved near upload: {first_debug_path}, {retry_debug_path}"
            ) from retry_error

    def _write_ingest_result(self, data: IngestLLMResult) -> list[str]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", data.slug):
            raise IngestServiceError(f"invalid generated source slug: {data.slug}")
        created_pages = [f"sources/{data.slug}.md"]
        self._atomic_write(self._wiki_dir / created_pages[0], data.source_page)

        for page in data.entity_pages:
            self._assert_allowed_wiki_output(page.path, "entities")
            self._atomic_write(self._wiki_dir / page.path, page.content)
            created_pages.append(page.path)

        for page in data.concept_pages:
            self._assert_allowed_wiki_output(page.path, "concepts")
            self._atomic_write(self._wiki_dir / page.path, page.content)
            created_pages.append(page.path)

        if data.overview_update:
            self._atomic_write(self._overview_file, data.overview_update)
        self._update_index(data.index_entry)
        self._append_log(data.log_entry)
        return created_pages

    def _build_prompt(self, *, source: Path, source_content: str) -> str:
        source_label = source.relative_to(self._agent_root).as_posix()
        return f"""You are maintaining an LLM Wiki. Process this source document and integrate its knowledge into the wiki.

Schema and conventions:
{self._read_text(self._schema_file)}

Current wiki state (index + recent pages):
{self._build_wiki_context() or "(wiki is empty - this is the first source)"}

New source to ingest (file: {source_label}):
=== SOURCE START ===
{source_content}
=== SOURCE END ===

Today's date: {date.today().isoformat()}

Return ONLY a valid JSON object with these fields (no markdown fences, no prose outside the JSON):
{{
  "title": "Human-readable title for this source",
  "slug": "kebab-case-slug-for-filename",
  "source_page": "full markdown content for wiki/sources/<slug>.md - aggressively convert key people, products, concepts and projects into [[Wikilinks]] inline",
  "index_entry": "- [Title](sources/slug.md) - one-line summary",
  "overview_update": null,
  "entity_pages": [
    {{"path": "entities/EntityName.md", "content": "full markdown content"}}
  ],
  "concept_pages": [
    {{"path": "concepts/ConceptName.md", "content": "full markdown content"}}
  ],
  "contradictions": ["describe any contradiction with existing wiki content, or empty list"],
  "log_entry": "## [{date.today().isoformat()}] ingest | <title>\\n\\nAdded source. Key claims: ..."
}}

Important:
- Always set "overview_update" to null. Do not rewrite wiki/overview.md in this response.
- Keep generated entity_pages and concept_pages focused. Prefer the source_page, index_entry, contradictions, and log_entry.
- Return complete JSON that can be parsed by json.loads; incomplete JSON is a failure.
"""

    def _build_wiki_context(self) -> str:
        parts: list[str] = []
        if self._index_file.exists():
            parts.append(
                f"## wiki/index.md\n{self._clip_text(self._read_text(self._index_file), MAX_INDEX_CONTEXT_CHARS)}"
            )
        if self._overview_file.exists():
            parts.append(
                "## wiki/overview.md\n"
                f"{self._clip_text(self._read_text(self._overview_file), MAX_OVERVIEW_CONTEXT_CHARS)}"
            )
        sources_dir = self._wiki_dir / "sources"
        if sources_dir.exists():
            recent_sources = sorted(
                sources_dir.glob("*.md"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:5]
            for path in recent_sources:
                parts.append(
                    f"## {path.relative_to(self._agent_root).as_posix()}\n"
                    f"{self._clip_text(self._read_text(path), MAX_RECENT_SOURCE_CONTEXT_CHARS)}"
                )
        return "\n\n---\n\n".join(parts)

    def _convert_to_markdown(self, source: Path) -> Path:
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise IngestServiceError("markitdown is not installed") from exc

        try:
            result = MarkItDown(enable_plugins=False).convert(str(source))
        except Exception as exc:
            raise IngestServiceError(f"failed to convert file: {source.name}") from exc

        output = source.with_suffix(".md")
        self._atomic_write(output, result.text_content)
        return output

    def _update_index(self, new_entry: str) -> None:
        content = self._read_text(self._index_file)
        if not content:
            content = (
                "# Wiki Index\n\n"
                "## Overview\n- [Overview](overview.md) - living synthesis\n\n"
                "## Sources\n\n## Entities\n\n## Concepts\n\n## Syntheses\n"
            )
        heading = "## Sources"
        if heading in content:
            content = content.replace(heading + "\n", heading + "\n" + new_entry + "\n", 1)
        else:
            suffix = "" if content.endswith("\n") else "\n"
            content = f"{content}{suffix}\n{heading}\n{new_entry}\n"
        self._atomic_write(self._index_file, content)

    def _append_log(self, entry: str) -> None:
        existing = self._read_text(self._log_file)
        self._atomic_write(self._log_file, entry.strip() + "\n\n" + existing)

    def _validate_ingest(self, changed_pages: list[str]) -> IngestValidation:
        existing_pages = {
            path.stem.lower()
            for path in self._wiki_dir.rglob("*.md")
            if path.name not in {"index.md", "log.md", "lint-report.md"}
        }
        index_content = self._read_text(self._index_file).lower()
        broken_links: list[tuple[str, str]] = []
        unindexed: list[str] = []

        for page in changed_pages:
            page_path = self._wiki_dir / page
            if not page_path.exists():
                continue
            content = self._read_text(page_path)
            for link in re.findall(r"\[\[([^\]]+)\]\]", content):
                link_stem = Path(link).stem.lower() if "/" in link else link.lower()
                if link_stem not in existing_pages:
                    broken_links.append((page, link))
            stem = page_path.stem.lower()
            if stem not in index_content and page not in {"log.md", "overview.md"}:
                unindexed.append(page)

        return IngestValidation(broken_links=broken_links, unindexed=unindexed)

    @staticmethod
    def _parse_json_from_response(text: str) -> dict[str, Any]:
        sanitized = re.sub(r"^```(?:json)?\s*", "", text.strip())
        sanitized = re.sub(r"\s*```$", "", sanitized.strip())
        match = re.search(r"\{[\s\S]*\}", sanitized)
        if not match:
            raise IngestServiceError("LLM response did not contain a JSON object")
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError as exc:
            raise IngestServiceError("LLM response was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise IngestServiceError("LLM response JSON must be an object")
        return parsed

    def _write_llm_debug_response(
        self,
        *,
        source_path: Path,
        job_id: str,
        label: str,
        content: str,
    ) -> str:
        debug_path = source_path.with_name(f"{source_path.stem}.{job_id}.{label}.llm-response.txt")
        self._atomic_write(debug_path, content)
        try:
            return debug_path.relative_to(self._agent_root).as_posix()
        except ValueError:
            return str(debug_path)

    def _call_llm_with_retry(self, prompt: str) -> str:
        call_llm_main = self._get_llm_caller()
        for attempt in range(2):
            try:
                return call_llm_main(prompt, max_tokens=8192)
            except Exception:
                if attempt == 1:
                    raise
                LOGGER.warning("LLM ingest generation failed; retrying once", exc_info=True)
                time.sleep(1)
        raise RuntimeError("unreachable LLM retry state")

    def _get_llm_caller(self) -> Callable[[str, int | None], str]:
        if self._call_llm_main is None:
            self._call_llm_main = self._load_llm_caller()
        assert self._call_llm_main is not None
        return self._call_llm_main

    def _load_llm_caller(self) -> Callable[[str, int | None], str]:
        if not self._agent_root.exists():
            raise IngestServiceError(f"llm-wiki-agent repo not found: {self._agent_root}")

        agent_root_text = str(self._agent_root)
        if agent_root_text not in sys.path:
            sys.path.insert(0, agent_root_text)

        try:
            llm_config = importlib.import_module("tools.llm_config")
        except ModuleNotFoundError as exc:
            raise IngestServiceError("Unable to import llm-wiki-agent tools.llm_config.") from exc

        call_llm_main = getattr(llm_config, "call_llm_main", None)
        if not callable(call_llm_main):
            raise IngestServiceError("llm-wiki-agent tools.llm_config is missing call_llm_main.")
        return call_llm_main

    @staticmethod
    def _safe_filename(value: str) -> str:
        name = Path(value).name
        name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "-", name)
        name = re.sub(r"\s+", "-", name.strip())
        name = name.strip(".-")
        return name[:180] or "upload"

    @staticmethod
    def _assert_allowed_wiki_output(relative_path: str, directory: str) -> None:
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts or path.parts[:1] != (directory,) or path.suffix != ".md":
            raise IngestServiceError(f"invalid generated wiki path: {relative_path}")

    @staticmethod
    def _read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @staticmethod
    def _clip_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        clipped = text[: limit - 80].rsplit("\n", 1)[0].rstrip()
        return f"{clipped}\n\n[context clipped to {limit} characters]"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)
        temp_path.replace(path)

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.utcnow().replace(microsecond=0)
