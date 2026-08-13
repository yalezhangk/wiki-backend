from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.migrate_ingest_source_origins import (
    _apply_migrations,
    _log_missing_files,
    _load_manual_migrations,
    _validate_plan,
)


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, tuple[Any, ...] | None]] = []
        self.last_query = ""

    def execute(self, query: str, values: tuple[Any, ...] | None = None) -> None:
        self.last_query = " ".join(query.split())
        self.queries.append((self.last_query, values))

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        if "WHERE `trigger` = 'manual'" in self.last_query:
            return [row for row in self.rows if row.get("trigger", "manual") == "manual"]
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


class _Storage:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cursor = _Cursor(rows)

    def connect(self) -> _Connection:
        return _Connection(self.cursor)


class SourceOriginMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.agent_root = Path(self.temp_dir.name)
        self.manual_path = self.agent_root / "raw" / "uploads" / "manual-source.md"
        self.scheduled_path = self.agent_root / "raw" / "uploads" / "scheduled-source.md"
        self.manual_path.parent.mkdir(parents=True)
        self.manual_path.write_text("manual", encoding="utf-8")
        self.manual_debug_path = self.manual_path.with_name("manual-source.10.initial.llm-response.txt")
        self.manual_debug_path.write_text("debug", encoding="utf-8")
        self.scheduled_path.write_text("scheduled", encoding="utf-8")
        source_page = self.agent_root / "wiki" / "sources" / "manual-source.md"
        source_page.parent.mkdir(parents=True)
        source_page.write_text(
            "---\nsource_file: raw/uploads/manual-source.md\n---\n",
            encoding="utf-8",
        )
        self.storage = _Storage(
            [
                {
                    "id": 10,
                    "status": "succeeded",
                    "stored_filename": "manual-source.md",
                    "source_path": "raw/uploads/manual-source.md",
                    "original_filename": "Manual Source.md",
                },
                {
                    "id": 20,
                    "status": "succeeded",
                    "trigger": "scheduled",
                    "stored_filename": "scheduled-source.md",
                    "source_path": "raw/uploads/scheduled-source.md",
                    "original_filename": "Scheduled Source.md",
                    "source_url": "https://example.com/scheduled-source",
                },
            ]
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_dry_run_plan_reads_only_manual_rows_and_writes_nothing(self) -> None:
        migrations = _load_manual_migrations(self.storage, self.agent_root)  # type: ignore[arg-type]
        _validate_plan(migrations=migrations, agent_root=self.agent_root)

        self.assertEqual(len(migrations), 1)
        self.assertEqual(migrations[0].new_path, "raw/uploads/manual/manual-source.md")
        self.assertTrue(self.manual_path.exists())
        self.assertEqual(self.scheduled_path.read_text(encoding="utf-8"), "scheduled")
        statements = "\n".join(query for query, _ in self.storage.cursor.queries)
        self.assertIn("WHERE `trigger` = 'manual'", statements)
        self.assertNotIn("UPDATE ingest_jobs", statements)

    def test_apply_moves_only_manual_file_and_updates_manual_source(self) -> None:
        migrations = _load_manual_migrations(self.storage, self.agent_root)  # type: ignore[arg-type]
        _validate_plan(migrations=migrations, agent_root=self.agent_root)

        _apply_migrations(
            storage_instance=self.storage,  # type: ignore[arg-type]
            migrations=migrations,
            agent_root=self.agent_root,
        )

        migrated_path = self.agent_root / "raw" / "uploads" / "manual" / "manual-source.md"
        self.assertFalse(self.manual_path.exists())
        self.assertEqual(migrated_path.read_text(encoding="utf-8"), "manual")
        self.assertFalse(self.manual_debug_path.exists())
        self.assertEqual(
            (migrated_path.parent / self.manual_debug_path.name).read_text(encoding="utf-8"),
            "debug",
        )
        self.assertEqual(self.scheduled_path.read_text(encoding="utf-8"), "scheduled")
        source_page = self.agent_root / "wiki" / "sources" / "manual-source.md"
        self.assertIn("source_file: raw/uploads/manual/manual-source.md", source_page.read_text(encoding="utf-8"))
        updates = [query for query, _ in self.storage.cursor.queries if "UPDATE ingest_jobs" in query]
        self.assertEqual(len(updates), 1)
        self.assertIn("WHERE id = %s AND `trigger` = 'manual'", updates[0])
        update_values = [values for query, values in self.storage.cursor.queries if "UPDATE ingest_jobs" in query]
        self.assertEqual(update_values, [("raw/uploads/manual/manual-source.md", "manual source", 10)])

    def test_failed_manual_job_without_cleaned_upload_does_not_block_apply(self) -> None:
        self.storage = _Storage(
            [
                {
                    "id": 11,
                    "status": "failed",
                    "stored_filename": "failed.md",
                    "source_path": "raw/uploads/failed.md",
                    "original_filename": "Failed.md",
                }
            ]
        )

        migrations = _load_manual_migrations(self.storage, self.agent_root)  # type: ignore[arg-type]
        _validate_plan(migrations=migrations, agent_root=self.agent_root)

        self.assertFalse(_log_missing_files(migrations=migrations, agent_root=self.agent_root))
        _apply_migrations(
            storage_instance=self.storage,  # type: ignore[arg-type]
            migrations=migrations,
            agent_root=self.agent_root,
        )

        updates = [query for query, _ in self.storage.cursor.queries if "UPDATE ingest_jobs" in query]
        self.assertEqual(len(updates), 1)


if __name__ == "__main__":
    unittest.main()
