from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.schemas.maintenance import MaintenanceJobResponse
from app.services.maintenance_service import MaintenanceTaskResult

STUB_THRESHOLD_CHARS = 100
_EXCLUDED_FILENAMES = {"index.md", "log.md", "lint-report.md", "health-report.md"}


class HealthMaintenanceStorage(Protocol):
    def update_maintenance_job_progress(
        self, *, job_id: int, stage: str, progress_percent: int, updated_at: datetime
    ) -> None:
        ...


@dataclass(frozen=True)
class HealthResult:
    report_date: str
    total_pages: int
    empty_files: list[dict[str, Any]]
    index_sync: dict[str, list[str]]
    log_coverage: list[dict[str, str]]

    def as_summary(self) -> dict[str, Any]:
        return {
            "scanned_page_count": self.total_pages,
            "empty_or_stub_count": len(self.empty_files),
            "index_difference_count": sum(len(value) for value in self.index_sync.values()),
            "log_missing_count": len(self.log_coverage),
            "report_date": self.report_date,
            "report_name": "health-report.md",
        }


class HealthMaintenanceService:
    """执行与 Agent health.py 兼容的确定性结构健康检查。"""

    def __init__(
        self,
        *,
        storage: HealthMaintenanceStorage,
        wiki_repo_path: Path,
        wiki_lock: threading.RLock,
    ) -> None:
        self._storage = storage
        self._repo_root = wiki_repo_path.resolve()
        self._wiki_dir = self._repo_root / "wiki"
        self._wiki_lock = wiki_lock

    def run(self, job: MaintenanceJobResponse) -> MaintenanceTaskResult:
        with self._wiki_lock:
            self._set_progress(job.job_id, "scanning_pages", 20)
            pages = self._all_wiki_pages()
            empty_files = self._check_empty_files(pages)
            self._set_progress(job.job_id, "checking_index", 45)
            index_sync = self._check_index_sync(pages)
            self._set_progress(job.job_id, "checking_log", 70)
            log_coverage = self._check_log_coverage()
            result = HealthResult(
                report_date=date.today().isoformat(),
                total_pages=len(pages),
                empty_files=empty_files,
                index_sync=index_sync,
                log_coverage=log_coverage,
            )
            if bool(job.options.get("save_report", True)):
                self._set_progress(job.job_id, "writing_report", 90)
                (self._wiki_dir / "health-report.md").write_text(
                    self._format_report(result), encoding="utf-8"
                )
        return MaintenanceTaskResult(result_summary=result.as_summary())

    def _all_wiki_pages(self) -> list[Path]:
        if not self._wiki_dir.is_dir():
            raise RuntimeError("Wiki directory is unavailable")
        return sorted(
            path
            for path in self._wiki_dir.rglob("*.md")
            if path.name not in _EXCLUDED_FILENAMES
        )

    def _check_empty_files(self, pages: list[Path]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in pages:
            raw = self._read_file(path)
            body = self._strip_frontmatter(raw)
            if len(body) < STUB_THRESHOLD_CHARS:
                results.append(
                    {
                        "path": self._repo_relative(path),
                        "total_bytes": len(raw),
                        "body_bytes": len(body),
                        "status": "empty" if not body else "stub",
                    }
                )
        return sorted(results, key=lambda item: int(item["body_bytes"]))

    def _check_index_sync(self, pages: list[Path]) -> dict[str, list[str]]:
        index_content = self._read_file(self._wiki_dir / "index.md")
        index_paths = {
            (self._wiki_dir / link).resolve()
            for link in re.findall(r"\[.*?\]\(([^)]+\.md)\)", index_content)
            if Path(link).name not in {"overview.md", "health-report.md", "lint-report.md"}
        }
        disk_paths = {
            path.resolve()
            for path in pages
            if path.name not in {"overview.md", "health-report.md", "lint-report.md"}
        }
        return {
            "in_index_not_on_disk": [self._repo_relative(path) for path in sorted(index_paths - disk_paths)],
            "on_disk_not_in_index": [self._repo_relative(path) for path in sorted(disk_paths - index_paths)],
        }

    def _check_log_coverage(self) -> list[dict[str, str]]:
        log_content = self._read_file(self._wiki_dir / "log.md")
        logged_titles = {
            match.group(1).strip().lower()
            for match in re.finditer(
                r"^## \[\d{4}-\d{2}-\d{2}\] ingest \| (.+)$", log_content, re.MULTILINE
            )
        }
        source_dir = self._wiki_dir / "sources"
        if not source_dir.is_dir():
            return []
        missing: list[dict[str, str]] = []
        for path in sorted(source_dir.glob("*.md")):
            slug = path.stem.lower().replace("-", " ").replace("_", " ")
            title = self._frontmatter_title(self._read_file(path))
            if slug not in logged_titles and title not in logged_titles:
                missing.append(
                    {"path": self._repo_relative(path), "slug": path.stem, "title": title or path.stem}
                )
        return missing

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                return content[end + 3 :].strip()
        return content.strip()

    @staticmethod
    def _frontmatter_title(content: str) -> str:
        match = re.search(r"^title:\s*(.+?)\s*$", content, re.MULTILINE)
        if match is None:
            return ""
        raw = match.group(1).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = raw[1:-1].replace(r'\"', '"').replace(r"\'", "'").replace(r"\\", "\\")
        elif len(raw) >= 2 and raw[0] == raw[-1] == "'":
            raw = raw[1:-1].replace("''", "'")
        return raw.strip().lower()

    def _format_report(self, result: HealthResult) -> str:
        lines = [
            f"# Wiki Health Report — {result.report_date}",
            "",
            f"Scanned {result.total_pages} wiki pages. Checks are purely structural (no LLM calls).",
            "",
            f"## Empty / Stub Files ({len(result.empty_files)} found)",
            "",
        ]
        if result.empty_files:
            lines.extend(["| Page | Total Bytes | Body Bytes | Status |", "|---|---|---|---|"])
            for item in result.empty_files:
                emoji = "🔴" if item["status"] == "empty" else "🟡"
                lines.append(
                    f"| `{item['path']}` | {item['total_bytes']} | {item['body_bytes']} | {emoji} {item['status']} |"
                )
        else:
            lines.append("All pages have content beyond frontmatter. ✅")
        lines.extend(["", f"## Index Sync ({sum(len(value) for value in result.index_sync.values())} issues)", ""])
        for heading, values in (
            ("### Stale Index Entries (in index.md but no file on disk)", result.index_sync["in_index_not_on_disk"]),
            ("### Missing from Index (file exists but not in index.md)", result.index_sync["on_disk_not_in_index"]),
        ):
            if values:
                lines.append(heading)
                lines.extend(f"- `{value}`" for value in values)
                lines.append("")
        if not any(result.index_sync.values()):
            lines.extend(["index.md is in sync with disk. ✅", ""])
        lines.extend([f"## Log Coverage ({len(result.log_coverage)} source pages without log entry)", ""])
        if result.log_coverage:
            lines.extend(["These source pages have no corresponding `ingest` entry in log.md:", ""])
            lines.extend(f"- `{item['path']}` — {item['title']}" for item in result.log_coverage)
        else:
            lines.append("All source pages have corresponding log entries. ✅")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _read_file(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _repo_relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self._repo_root)).replace("\\", "/")
        except ValueError as exc:
            raise RuntimeError("Wiki path is outside the configured repository") from exc

    def _set_progress(self, job_id: int, stage: str, progress_percent: int) -> None:
        self._storage.update_maintenance_job_progress(
            job_id=job_id,
            stage=stage,
            progress_percent=progress_percent,
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0),
        )
