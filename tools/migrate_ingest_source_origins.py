"""仅迁移历史 manual Ingest 的原文件路径和名称键，默认 dry-run。"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.config import settings
from app.services.ingest_document_name import normalize_document_name
from app.storage.mysql import MySQLStorage, storage

LOGGER = logging.getLogger(__name__)
SOURCE_FILE_PATTERN = re.compile(r"(?m)^source_file:\s*[^\r\n]+$")


@dataclass(frozen=True)
class ManualMigration:
    job_id: int
    old_path: str
    new_path: str
    name_key: str | None
    source_page: Path | None


def _agent_root() -> Path:
    return Path(settings.llm_wiki_repo_path).expanduser().resolve()


def _relative_manual_target(row: dict[str, Any]) -> str:
    filename = Path(str(row["stored_filename"])).name
    if not filename:
        raise ValueError(f"manual job {row['id']} has an empty stored filename")
    return (Path("raw") / "uploads" / "manual" / filename).as_posix()


def _source_page_for_job(*, agent_root: Path, old_path: str) -> Path | None:
    sources = agent_root / "wiki" / "sources"
    if not sources.is_dir():
        return None
    for page in sources.glob("*.md"):
        content = page.read_text(encoding="utf-8")
        if re.search(rf"(?m)^source_file:\s*{re.escape(old_path)}\s*$", content):
            return page
    return None


def _load_manual_migrations(storage_instance: MySQLStorage, agent_root: Path) -> list[ManualMigration]:
    with storage_instance.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, status, stored_filename, source_path, original_filename
                FROM ingest_jobs
                WHERE `trigger` = 'manual'
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()
    migrations: list[ManualMigration] = []
    for row in rows:
        old_path = str(row["source_path"])
        name_key = normalize_document_name(str(row["original_filename"])) if row["status"] != "failed" else None
        migrations.append(
            ManualMigration(
                job_id=int(row["id"]),
                old_path=old_path,
                new_path=_relative_manual_target(row),
                name_key=name_key,
                source_page=_source_page_for_job(agent_root=agent_root, old_path=old_path),
            )
        )
    return migrations


def _validate_plan(*, migrations: list[ManualMigration], agent_root: Path) -> None:
    seen_names: set[str] = set()
    seen_targets: set[str] = set()
    upload_root = (agent_root / "raw" / "uploads").resolve()
    for migration in migrations:
        old_file = (agent_root / migration.old_path).resolve()
        if old_file != upload_root and upload_root not in old_file.parents:
            raise ValueError(f"manual job {migration.job_id} source_path is outside raw/uploads")
        if migration.name_key is not None:
            if migration.name_key in seen_names:
                raise ValueError(f"manual internal duplicate document name: {migration.name_key}")
            seen_names.add(migration.name_key)
        if migration.new_path in seen_targets:
            raise ValueError(f"manual path conflict: {migration.new_path}")
        seen_targets.add(migration.new_path)
        target = agent_root / migration.new_path
        if target.exists() and target.resolve() != old_file:
            raise ValueError(f"manual target already exists: {migration.new_path}")
        for old_companion, new_companion in _confirmed_companion_moves(migration, agent_root):
            if new_companion.exists() and new_companion.resolve() != old_companion.resolve():
                raise ValueError(f"manual companion target already exists: {new_companion}")


def _confirmed_companion_moves(
    migration: ManualMigration, agent_root: Path
) -> list[tuple[Path, Path]]:
    """仅识别可由当前 job ID 和原文件主名唯一归属的工作产物。"""
    old_file = agent_root / migration.old_path
    new_file = agent_root / migration.new_path
    candidates: list[Path] = []
    if old_file.suffix.lower() != ".md":
        converted = old_file.with_suffix(".md")
        if converted.is_file():
            candidates.append(converted)
    candidates.extend(old_file.parent.glob(f"{old_file.stem}.{migration.job_id}.*.llm-response.txt"))
    return [
        (candidate, new_file.parent / candidate.name)
        for candidate in candidates
        if candidate.is_file() and candidate.resolve() != (new_file.parent / candidate.name).resolve()
    ]


def _log_missing_files(*, migrations: list[ManualMigration], agent_root: Path) -> bool:
    missing = False
    for migration in migrations:
        old_file = agent_root / migration.old_path
        new_file = agent_root / migration.new_path
        if not old_file.exists() and not new_file.exists():
            if migration.name_key is None:
                LOGGER.warning(
                    "failed manual job_id=%s source file is absent; no move is required old=%s new=%s",
                    migration.job_id,
                    migration.old_path,
                    migration.new_path,
                )
            else:
                LOGGER.error(
                    "manual job_id=%s source file is missing old=%s new=%s",
                    migration.job_id,
                    migration.old_path,
                    migration.new_path,
                )
                missing = True
    return missing


def _rewrite_source_page(*, page: Path, new_path: str) -> str:
    original = page.read_text(encoding="utf-8")
    replacement = f"source_file: {new_path}"
    updated, count = SOURCE_FILE_PATTERN.subn(replacement, original, count=1)
    if count != 1:
        raise ValueError(f"cannot update manual Source page: {page}")
    return updated


def _apply_migrations(
    *, storage_instance: MySQLStorage, migrations: list[ManualMigration], agent_root: Path
) -> None:
    moved: list[tuple[Path, Path]] = []
    page_backups: dict[Path, str] = {}
    try:
        with storage_instance.connect() as connection:
            with connection.cursor() as cursor:
                for migration in migrations:
                    old_file = agent_root / migration.old_path
                    new_file = agent_root / migration.new_path
                    if old_file.exists() and old_file.resolve() != new_file.resolve():
                        new_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(old_file), str(new_file))
                        moved.append((new_file, old_file))
                    for old_companion, new_companion in _confirmed_companion_moves(migration, agent_root):
                        new_companion.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(old_companion), str(new_companion))
                        moved.append((new_companion, old_companion))
                    if migration.source_page is not None:
                        page_backups[migration.source_page] = migration.source_page.read_text(encoding="utf-8")
                        migration.source_page.write_text(
                            _rewrite_source_page(page=migration.source_page, new_path=migration.new_path),
                            encoding="utf-8",
                        )
                    cursor.execute(
                        """
                        UPDATE ingest_jobs
                        SET source_path = %s, document_name_key = %s
                        WHERE id = %s AND `trigger` = 'manual'
                        """,
                        (migration.new_path, migration.name_key, migration.job_id),
                    )
    except Exception:
        for page, content in page_backups.items():
            page.write_text(content, encoding="utf-8")
        for new_file, old_file in reversed(moved):
            if new_file.exists():
                old_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(new_file), str(old_file))
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="确认执行 manual-only 迁移；默认仅 dry-run。")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    agent_root = _agent_root()
    migrations = _load_manual_migrations(storage, agent_root)
    _validate_plan(migrations=migrations, agent_root=agent_root)
    for migration in migrations:
        LOGGER.info(
            "manual job_id=%s old=%s new=%s source_page=%s document_name_key=%s",
            migration.job_id,
            migration.old_path,
            migration.new_path,
            migration.source_page.relative_to(agent_root).as_posix() if migration.source_page else "(none)",
            migration.name_key,
        )
    if _log_missing_files(migrations=migrations, agent_root=agent_root):
        LOGGER.error("migration stopped because one or more manual source files are missing")
        return 2
    if not arguments.apply:
        LOGGER.info("dry-run complete; no files or database rows were changed")
        return 0
    _apply_migrations(storage_instance=storage, migrations=migrations, agent_root=agent_root)
    LOGGER.info("manual-only source-origin migration complete count=%s", len(migrations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
