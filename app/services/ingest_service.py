from __future__ import annotations

import json
import logging
import re
import threading
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from queue import Queue
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Protocol

from fastapi import UploadFile
from pydantic import ValidationError

from app.config import settings
from app.llm_config import LLMConfigError, LLMResponseTruncatedError, call_llm_main
from app.prompts import load_prompt, render_prompt
from app.schemas.ingest import IngestJobResponse, IngestLLMResult, IngestValidation
from app.services.publish_service import PublishService
from app.services.wiki_page_policy import iter_knowledge_pages
from app.time_utils import beijing_now

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
UPLOAD_CHUNK_BYTES = 64 * 1024
GENERIC_CONTENT_TYPES = {"", "application/octet-stream"}
ALLOWED_CONTENT_TYPES = {
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    },
    ".pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip",
    },
    ".xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
    },
    ".xls": {"application/vnd.ms-excel"},
    ".html": {"text/html", "application/xhtml+xml"},
    ".htm": {"text/html", "application/xhtml+xml"},
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/x-markdown", "text/plain"},
    ".csv": {"text/csv", "application/csv", "application/vnd.ms-excel", "text/plain"},
    ".json": {"application/json", "text/json", "text/plain"},
    ".xml": {"application/xml", "text/xml", "text/plain"},
    ".rst": {"text/x-rst", "text/plain"},
    ".rtf": {"application/rtf", "application/x-rtf", "text/rtf", "text/plain"},
    ".epub": {"application/epub+zip", "application/zip"},
    ".ipynb": {"application/json", "application/x-ipynb+json", "text/json", "text/plain"},
    ".yaml": {"application/yaml", "application/x-yaml", "text/yaml", "text/x-yaml", "text/plain"},
    ".yml": {"application/yaml", "application/x-yaml", "text/yaml", "text/x-yaml", "text/plain"},
    ".tsv": {"text/tab-separated-values", "text/plain"},
    ".wav": {"audio/wav", "audio/x-wav", "audio/wave"},
    ".mp3": {"audio/mpeg", "audio/mp3"},
}
ZIP_REQUIRED_PREFIXES = {
    ".docx": "word/",
    ".pptx": "ppt/",
    ".xlsx": "xl/",
}


class IngestServiceError(RuntimeError):
    """Raised when an ingest request cannot be accepted."""


class IngestValidationError(IngestServiceError):
    """Raised when an uploaded file is invalid."""


class IngestConflictError(IngestServiceError):
    """Raised when the target upload path already exists."""


class IngestNotFoundError(IngestServiceError):
    """Raised when an ingest job cannot be found."""


class IngestLLMResponseError(IngestServiceError):
    """Raised when an LLM response cannot be safely used for ingest."""

    def __init__(self, category: str, user_message: str) -> None:
        self.category = category
        self.user_message = user_message
        super().__init__(f"{category}: {user_message}")


class IngestLLMResponseTruncatedError(IngestLLMResponseError):
    """Raised when the provider or JSON decoder identifies a truncated response."""

    def __init__(self, *, response_content: str | None = None) -> None:
        self.response_content = response_content
        super().__init__(
            "llm_response_truncated",
            "模型输出因长度限制被截断，请调整文档或输出预算后重试。",
        )


class IngestLLMSchemaError(IngestLLMResponseError):
    """Raised when JSON is complete but does not meet the ingest contract."""

    def __init__(self) -> None:
        super().__init__(
            "llm_schema_invalid",
            "模型返回的数据不符合入库格式，请检查服务日志后重试。",
        )


class IngestLLMInvalidJSONError(IngestLLMResponseError):
    """Raised when a complete LLM response cannot be decoded as an object."""

    def __init__(self) -> None:
        super().__init__(
            "llm_json_invalid",
            "模型返回的入库数据不是有效 JSON，请检查服务日志后重试。",
        )


class IngestStorage(Protocol):
    def create_ingest_job(
        self,
        *,
        status: str,
        original_filename: str,
        stored_filename: str,
        source_path: str,
        created_at: datetime,
    ) -> IngestJobResponse:
        ...

    def get_ingest_job(self, job_id: int) -> IngestJobResponse | None:
        ...

    def list_ingest_jobs(self, limit: int) -> list[IngestJobResponse]:
        ...

    def mark_ingest_job_running(self, job_id: int, started_at: datetime) -> None:
        ...

    def update_ingest_job_progress(
        self,
        *,
        job_id: int,
        stage: str,
        progress_percent: int,
        updated_at: datetime,
    ) -> None:
        ...

    def mark_ingest_job_succeeded(
        self,
        *,
        job_id: int,
        created_pages: list[str],
        updated_pages: list[str],
        contradictions: list[str],
        validation: IngestValidation,
        finished_at: datetime,
    ) -> None:
        ...

    def mark_ingest_job_failed(self, *, job_id: int, error: str, finished_at: datetime) -> None:
        ...


class IngestService:
    def __init__(
        self,
        *,
        storage: IngestStorage,
        agent_root: Path,
        start_worker: bool = True,
        publish_service: PublishService | None = None,
        wiki_lock: Any | None = None,
        max_upload_bytes: int = settings.ingest_max_upload_bytes,
        ingest_llm_max_tokens: int = settings.ingest_llm_max_tokens,
    ) -> None:
        self._storage = storage
        self._agent_root = agent_root.resolve()
        self._wiki_dir = self._agent_root / "wiki"
        self._upload_dir = self._agent_root / "raw" / "uploads"
        self._index_file = self._wiki_dir / "index.md"
        self._overview_file = self._wiki_dir / "overview.md"
        self._log_file = self._wiki_dir / "log.md"
        self._call_llm_main: Callable[[str, int | None], str] | None = None
        self._last_llm_result_raw: str | None = None
        self._max_upload_bytes = max_upload_bytes
        self._ingest_llm_max_tokens = ingest_llm_max_tokens
        self._publish_service = publish_service
        self._wiki_lock = wiki_lock or threading.RLock()
        self._queue: Queue[int] = Queue()
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

        stored_filename = self._safe_filename(original_filename)
        relative_source_path = Path("raw") / "uploads" / stored_filename
        target_path = self._agent_root / relative_source_path
        if target_path.exists():
            raise IngestConflictError(
                f"上传文件已存在，请修改文件名后重试: {relative_source_path.as_posix()}"
            )

        self._upload_dir.mkdir(parents=True, exist_ok=True)
        await self._save_upload(file=file, target_path=target_path, suffix=suffix)

        created_at = self._beijing_now()
        job = self._storage.create_ingest_job(
            status="queued",
            original_filename=original_filename,
            stored_filename=stored_filename,
            source_path=relative_source_path.as_posix(),
            created_at=created_at,
        )
        self._queue.put(job.job_id)
        return job

    def get_job(self, job_id: int) -> IngestJobResponse:
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

    def _run_job(self, job_id: int) -> None:
        job = self.get_job(job_id)
        started_at = self._beijing_now()
        self._storage.mark_ingest_job_running(job_id, started_at)
        try:
            result = self._ingest_source(self._agent_root / job.source_path, job_id=job_id)
            self._storage.mark_ingest_job_succeeded(
                job_id=job_id,
                created_pages=result["created_pages"],
                updated_pages=result["updated_pages"],
                contradictions=result["contradictions"],
                validation=result["validation"],
                finished_at=self._beijing_now(),
            )
            if self._publish_service is not None:
                try:
                    self._publish_service.queue_change(source_kind="ingest", source_id=str(job_id))
                except Exception:
                    LOGGER.exception("Failed to queue Quartz publish after ingest job_id=%s", job_id)
        except Exception as exc:
            LOGGER.exception("Ingest job failed job_id=%s", job_id)
            self._storage.mark_ingest_job_failed(
                job_id=job_id,
                error=str(exc),
                finished_at=self._beijing_now(),
            )

    def _ingest_source(self, source_path: Path, *, job_id: int) -> dict[str, Any]:
        source = source_path
        if source_path.suffix.lower() != ".md":
            self._update_progress(job_id, "converting", 10)
            source = self._convert_to_markdown(source_path)

        self._update_progress(job_id, "extracting", 35)
        source_content = source.read_text(encoding="utf-8")
        prompt = self._build_prompt(source=source, source_content=source_content)
        try:
            raw = self._call_llm_with_retry(prompt)
        except IngestLLMResponseTruncatedError as exc:
            if exc.response_content:
                debug_path = self._write_llm_debug_response(
                    source_path=source_path,
                    job_id=job_id,
                    label="truncated",
                    content=exc.response_content,
                )
                LOGGER.warning(
                    "LLM ingest response was truncated job_id=%s debug_path=%s",
                    job_id,
                    debug_path,
                )
            raise
        parsed = self._parse_llm_result_with_repair(
            prompt=prompt,
            raw=raw,
            source_path=source_path,
            job_id=job_id,
        )
        try:
            data = IngestLLMResult.model_validate(parsed)
        except ValidationError as exc:
            debug_path = self._write_llm_debug_response(
                source_path=source_path,
                job_id=job_id,
                label="schema",
                content=self._last_llm_result_raw or raw,
            )
            LOGGER.warning(
                "LLM ingest JSON failed schema validation job_id=%s debug_path=%s",
                job_id,
                debug_path,
                exc_info=True,
            )
            raise IngestLLMSchemaError() from exc

        self._update_progress(job_id, "writing_wiki", 65)
        with self._wiki_lock:
            created_pages, updated_pages, changed_knowledge_pages = self._write_ingest_result(data)
        self._update_progress(job_id, "validating", 85)
        validation = self._validate_ingest(changed_knowledge_pages)

        return {
            "created_pages": created_pages,
            "updated_pages": updated_pages,
            "contradictions": data.contradictions,
            "validation": validation,
        }


    def _update_progress(self, job_id: int, stage: str, progress_percent: int) -> None:
        self._storage.update_ingest_job_progress(
            job_id=job_id,
            stage=stage,
            progress_percent=progress_percent,
            updated_at=self._beijing_now(),
        )

    def _parse_llm_result_with_repair(
        self,
        *,
        prompt: str,
        raw: str,
        source_path: Path,
        job_id: int,
    ) -> dict[str, Any]:
        self._last_llm_result_raw = raw
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
                "LLM ingest response could not be parsed job_id=%s category=%s debug_path=%s",
                job_id,
                getattr(first_error, "category", "llm_json_invalid"),
                first_debug_path,
            )

            if isinstance(first_error, IngestLLMResponseTruncatedError):
                raise

        repair_prompt = (
            f"{prompt}\n\n"
            "The response below could not be parsed as a JSON object. Repair its JSON "
            "structure while preserving its information, then return the complete result as raw JSON only. "
            "Do not include markdown fences, explanations, apologies, analysis, or any text outside the JSON object."
            "\n\n=== INVALID RESPONSE START ===\n"
            f"{raw}\n"
            "=== INVALID RESPONSE END ==="
        )
        retry_raw = self._call_llm_with_retry(repair_prompt)
        self._last_llm_result_raw = retry_raw
        try:
            return self._parse_json_from_response(retry_raw)
        except IngestServiceError as retry_error:
            retry_debug_path = self._write_llm_debug_response(
                source_path=source_path,
                job_id=job_id,
                label="retry",
                content=retry_raw,
            )
            LOGGER.warning(
                "LLM ingest repair response could not be parsed job_id=%s category=%s debug_path=%s",
                job_id,
                getattr(retry_error, "category", "llm_json_invalid"),
                retry_debug_path,
            )
            raise IngestLLMInvalidJSONError() from retry_error

    def _write_ingest_result(self, data: IngestLLMResult) -> tuple[list[str], list[str], list[str]]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", data.slug):
            raise IngestServiceError(f"invalid generated source slug: {data.slug}")

        source_path = f"sources/{data.slug}.md"
        generated_pages = [(source_path, data.source_page)]

        for page in data.entity_pages:
            self._assert_allowed_wiki_output(page.path, "entities")
            generated_pages.append((page.path, page.content))

        for page in data.concept_pages:
            self._assert_allowed_wiki_output(page.path, "concepts")
            generated_pages.append((page.path, page.content))

        changed_knowledge_pages = list(dict.fromkeys(path for path, _ in generated_pages))
        changed_pages = [*changed_knowledge_pages, "index.md", "log.md"]
        if data.overview_update:
            changed_pages.append("overview.md")
        existed_before = {
            path: (self._wiki_dir / path).exists()
            for path in changed_pages
        }

        for path, content in generated_pages:
            self._atomic_write(self._wiki_dir / path, content)

        if data.overview_update:
            self._atomic_write(self._overview_file, data.overview_update)
        self._update_index(data.index_entry)
        self._append_log(data.log_entry)
        created_pages = [path for path in changed_pages if not existed_before[path]]
        updated_pages = [path for path in changed_pages if existed_before[path]]
        return created_pages, updated_pages, changed_knowledge_pages

    async def _save_upload(self, *, file: UploadFile, target_path: Path, suffix: str) -> None:
        content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
        allowed_types = ALLOWED_CONTENT_TYPES.get(suffix, set())
        if content_type not in GENERIC_CONTENT_TYPES and content_type not in allowed_types:
            raise IngestValidationError(
                f"content type {content_type!r} does not match file extension {suffix}"
            )

        total_bytes = 0
        header = b""
        try:
            with target_path.open("xb") as output:
                while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                    total_bytes += len(chunk)
                    if total_bytes > self._max_upload_bytes:
                        raise IngestValidationError(
                            f"file exceeds maximum upload size of {self._max_upload_bytes} bytes"
                        )
                    if len(header) < 16:
                        header = (header + chunk)[:16]
                    output.write(chunk)
            if total_bytes == 0:
                raise IngestValidationError("file cannot be empty")
            self._validate_file_signature(target_path, suffix, header)
        except FileExistsError as exc:
            raise IngestConflictError(
                f"上传文件已存在，请修改文件名后重试: {target_path.name}"
            ) from exc
        except Exception:
            try:
                target_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Failed to remove rejected upload path=%s", target_path, exc_info=True)
            raise

    @staticmethod
    def _validate_file_signature(path: Path, suffix: str, header: bytes) -> None:
        if suffix == ".pdf" and not header.startswith(b"%PDF-"):
            raise IngestValidationError("file content does not match .pdf extension")
        if suffix == ".xls" and not header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise IngestValidationError("file content does not match .xls extension")
        if suffix == ".wav" and not (header.startswith(b"RIFF") and header[8:12] == b"WAVE"):
            raise IngestValidationError("file content does not match .wav extension")
        if suffix == ".mp3" and not (
            header.startswith(b"ID3")
            or (
                len(header) >= 2
                and header[0] == 0xFF
                and header[1] & 0xE0 == 0xE0
            )
        ):
            raise IngestValidationError("file content does not match .mp3 extension")
        if suffix == ".rtf" and not header.lstrip().startswith(b"{\\rtf"):
            raise IngestValidationError("file content does not match .rtf extension")
        if suffix in ZIP_REQUIRED_PREFIXES:
            try:
                with zipfile.ZipFile(path) as archive:
                    required_prefix = ZIP_REQUIRED_PREFIXES[suffix]
                    if not any(name.startswith(required_prefix) for name in archive.namelist()):
                        raise IngestValidationError(f"file content does not match {suffix} extension")
            except zipfile.BadZipFile as exc:
                raise IngestValidationError(f"file content does not match {suffix} extension") from exc
        if suffix == ".epub":
            try:
                with zipfile.ZipFile(path) as archive:
                    with archive.open("mimetype") as mimetype_file:
                        mimetype = mimetype_file.read(64)
                    if mimetype != b"application/epub+zip":
                        raise IngestValidationError("file content does not match .epub extension")
            except (KeyError, zipfile.BadZipFile) as exc:
                raise IngestValidationError("file content does not match .epub extension") from exc

    def _build_prompt(self, *, source: Path, source_content: str) -> str:
        source_label = source.relative_to(self._agent_root).as_posix()
        return render_prompt(
            "ingest.md",
            schema=load_prompt("agent_instructions.md"),
            wiki_context=self._build_wiki_context() or "(wiki is empty - this is the first source)",
            source_label=source_label,
            source_content=source_content,
            today=date.today().isoformat(),
        )

    def _build_wiki_context(self) -> str:
        parts: list[str] = []
        if self._index_file.exists():
            parts.append(f"## wiki/index.md\n{self._read_text(self._index_file)}")
        if self._overview_file.exists():
            parts.append(f"## wiki/overview.md\n{self._read_text(self._overview_file)}")
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
                    f"{self._read_text(path)}"
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
        formatted_entry = entry.strip()
        header = re.search(r"(?m)^# Wiki Log[ \t]*(?:\r?\n|$)", existing)
        if header is not None:
            separator = re.search(
                r"(?m)^---[ \t]*(?:\r?\n|$)",
                existing[header.end() :],
            )
            if separator is not None:
                separator_end = header.end() + separator.end()
                template = existing[header.start() : separator_end].rstrip("\r\n")
                misplaced_entries = existing[: header.start()].strip()
                previous_entries = existing[separator_end:].strip()
                entry_blocks = [
                    block
                    for block in (formatted_entry, misplaced_entries, previous_entries)
                    if block
                ]
                content = f"{template}\n\n" + "\n\n".join(entry_blocks) + "\n"
                self._atomic_write(self._log_file, content)
                return
        self._atomic_write(self._log_file, formatted_entry + "\n\n" + existing)

    def _validate_ingest(self, changed_pages: list[str]) -> IngestValidation:
        existing_pages = {path.stem.lower() for path in iter_knowledge_pages(self._wiki_dir)}
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
        start = sanitized.find("{")
        if start < 0:
            raise IngestLLMInvalidJSONError()
        try:
            parsed, _ = json.JSONDecoder().raw_decode(sanitized[start:])
        except json.JSONDecodeError as exc:
            if "unterminated" in exc.msg.lower() or exc.pos >= max(0, len(sanitized[start:]) - 1):
                raise IngestLLMResponseTruncatedError() from exc
            raise IngestLLMInvalidJSONError() from exc
        if not isinstance(parsed, dict):
            raise IngestLLMInvalidJSONError()
        return parsed

    def _write_llm_debug_response(
        self,
        *,
        source_path: Path,
        job_id: int,
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
                return call_llm_main(prompt, max_tokens=self._ingest_llm_max_tokens)
            except LLMResponseTruncatedError as exc:
                raise IngestLLMResponseTruncatedError(
                    response_content=exc.response_content,
                ) from exc
            except Exception as exc:
                if attempt == 1 or not self._is_transient_llm_error(exc):
                    raise
                LOGGER.warning(
                    "Transient LLM ingest generation failure; retrying once error_type=%s",
                    type(exc).__name__,
                )
                time.sleep(0.25)
        raise RuntimeError("unreachable LLM retry state")

    @staticmethod
    def _is_transient_llm_error(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        if isinstance(exc, LLMConfigError):
            return str(exc) == "LLM returned an empty response"
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return status_code == 429 or 500 <= status_code <= 599
        name = type(exc).__name__.lower()
        return "timeout" in name or "connection" in name or "ratelimit" in name

    def _get_llm_caller(self) -> Callable[[str, int | None], str]:
        if self._call_llm_main is None:
            self._call_llm_main = self._load_llm_caller()
        assert self._call_llm_main is not None
        return self._call_llm_main

    def _load_llm_caller(self) -> Callable[[str, int | None], str]:
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
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)
        temp_path.replace(path)

    @staticmethod
    def _beijing_now() -> datetime:
        return beijing_now()
