from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import create_app
from app.schemas.chat import ChatMessageResponse
from app.schemas.ingest import IngestValidation
from app.schemas.query import CitationResponse, QueryResult
from app.services.chat_service import ChatService
from app.services.chat_turn_service import ChatTurnService
from app.storage.mysql import MySQLStorage


class StubQueryService:
    def run_chat_turn(
        self,
        question: str,
        history_messages: list[ChatMessageResponse],
    ) -> QueryResult:
        return QueryResult(
            answer=f"## 数据库联调回答\n\n- 问题\n  - {question}\n\n```text\n保留缩进\n```",
            sources=["MySQL联调来源"],
            relevant_pages=["integration/mysql.md"],
            citations=[
                CitationResponse(
                    path="integration/mysql.md",
                    title="MySQL 联调来源",
                    kind="page",
                )
            ],
        )


@unittest.skipUnless(
    os.getenv("WIKI_BACKEND_RUN_MYSQL_INTEGRATION") == "1",
    "MySQL integration test is disabled",
)
class MySQLIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MySQLStorage(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=settings.mysql_database,
        )
        self.storage.initialize()
        self.created_chat_ids: list[int] = []
        self.created_ingest_job_ids: list[int] = []

        chat_service = ChatService(self.storage)
        turn_service = ChatTurnService(
            chat_service=chat_service,
            query_service=StubQueryService(),  # type: ignore[arg-type]
            history_limit=settings.chat_history_limit,
        )
        self.client = TestClient(
            create_app(
                chat_service=chat_service,
                chat_turn_service=turn_service,
                initialize_storage=False,
            )
        )

    def tearDown(self) -> None:
        with self.storage.connect() as connection:
            with connection.cursor() as cursor:
                if self.created_ingest_job_ids:
                    placeholders = ", ".join(["%s"] * len(self.created_ingest_job_ids))
                    cursor.execute(
                        f"DELETE FROM ingest_jobs WHERE id IN ({placeholders})",
                        tuple(self.created_ingest_job_ids),
                    )
                if self.created_chat_ids:
                    placeholders = ", ".join(["%s"] * len(self.created_chat_ids))
                    cursor.execute(
                        f"DELETE FROM chats WHERE id IN ({placeholders})",
                        tuple(self.created_chat_ids),
                    )

    def test_chat_api_round_trip_with_real_mysql(self) -> None:
        first_response = self.client.post("/api/chats")
        self.assertEqual(first_response.status_code, 200)
        first_chat = first_response.json()
        self.created_chat_ids.append(first_chat["id"])
        self.assertIsInstance(first_chat["id"], int)
        self.assertEqual(first_chat["title"], "新对话")

        second_response = self.client.post("/api/chats", json={"title": "待重命名"})
        self.assertEqual(second_response.status_code, 200)
        second_chat = second_response.json()
        self.created_chat_ids.append(second_chat["id"])
        self.assertIsInstance(second_chat["id"], int)

        turn_response = self.client.post(
            f"/api/chats/{first_chat['id']}/messages",
            json={"content": "MySQL 能保存多轮会话吗？\n  保留缩进"},
        )
        self.assertEqual(turn_response.status_code, 200)
        turn = turn_response.json()
        self.assertEqual(turn["chat"]["title"], "MySQL 能保存多轮会话吗？ 保留缩进")
        self.assertEqual(turn["assistant_message"]["sources"], ["MySQL联调来源"])
        self.assertEqual(
            turn["assistant_message"]["relevant_pages"],
            ["integration/mysql.md"],
        )

        messages_response = self.client.get(f"/api/chats/{first_chat['id']}/messages")
        self.assertEqual(messages_response.status_code, 200)
        messages = messages_response.json()["messages"]
        self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], "MySQL 能保存多轮会话吗？\n  保留缩进")
        self.assertEqual(
            messages[1]["content"],
            "## 数据库联调回答\n\n- 问题\n  - MySQL 能保存多轮会话吗？\n  保留缩进"
            "\n\n```text\n保留缩进\n```",
        )
        self.assertEqual(messages[1]["sources"], ["MySQL联调来源"])
        self.assertEqual(messages[1]["citations"][0]["path"], "integration/mysql.md")

        rename_response = self.client.patch(
            f"/api/chats/{second_chat['id']}",
            json={"title": "已完成重命名"},
        )
        self.assertEqual(rename_response.status_code, 200)
        self.assertEqual(rename_response.json()["title"], "已完成重命名")

        second_chat_updated_at = datetime.utcnow().replace(microsecond=0) + timedelta(seconds=1)
        self.storage.update_chat_activity(
            second_chat["id"],
            updated_at=second_chat_updated_at,
            last_message_at=None,
        )

        chats_response = self.client.get("/api/chats")
        self.assertEqual(chats_response.status_code, 200)
        created_ids = [
            chat["id"]
            for chat in chats_response.json()
            if chat["id"] in self.created_chat_ids
        ]
        self.assertEqual(created_ids, [second_chat["id"], first_chat["id"]])

    def test_tables_and_columns_have_comments(self) -> None:
        expected_column_counts = {
            "chats": 6,
            "chat_messages": 10,
            "maintenance_jobs": 16,
            "maintenance_page_state": 6,
            "maintenance_findings": 10,
        }
        with self.storage.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT TABLE_NAME, TABLE_COMMENT
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME IN (
                          'chats', 'chat_messages', 'maintenance_jobs',
                          'maintenance_page_state', 'maintenance_findings'
                      )
                    """,
                    (settings.mysql_database,),
                )
                table_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_COMMENT
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME IN (
                          'chats', 'chat_messages', 'maintenance_jobs',
                          'maintenance_page_state', 'maintenance_findings'
                      )
                    """,
                    (settings.mysql_database,),
                )
                column_rows = cursor.fetchall()

        self.assertEqual(len(table_rows), len(expected_column_counts))
        self.assertTrue(all(row["TABLE_COMMENT"] for row in table_rows))
        for table_name, expected_count in expected_column_counts.items():
            comments = [
                row["COLUMN_COMMENT"]
                for row in column_rows
                if row["TABLE_NAME"] == table_name
            ]
            self.assertEqual(len(comments), expected_count)
            self.assertTrue(all(comments))

    def test_temporal_columns_have_second_precision(self) -> None:
        expected_columns = {
            ("chats", "created_at"),
            ("chats", "updated_at"),
            ("chats", "last_message_at"),
            ("chat_messages", "created_at"),
            ("chat_messages", "synthesized_at"),
            ("ingest_jobs", "created_at"),
            ("ingest_jobs", "started_at"),
            ("ingest_jobs", "updated_at"),
            ("ingest_jobs", "finished_at"),
        }
        with self.storage.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_NAME, DATETIME_PRECISION
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME IN ('chats', 'chat_messages', 'ingest_jobs')
                      AND DATA_TYPE = 'datetime'
                    """,
                    (settings.mysql_database,),
                )
                rows = cursor.fetchall()

        actual_columns = {
            (row["TABLE_NAME"], row["COLUMN_NAME"]): row["DATETIME_PRECISION"]
            for row in rows
        }
        self.assertEqual(set(actual_columns), expected_columns)
        self.assertTrue(all(precision == 0 for precision in actual_columns.values()))

    def test_ingest_jobs_has_persisted_progress_columns(self) -> None:
        with self.storage.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME = 'ingest_jobs'
                      AND COLUMN_NAME IN ('stage', 'progress_percent', 'updated_at')
                    """,
                    (settings.mysql_database,),
                )
                rows = cursor.fetchall()

        columns = {row["COLUMN_NAME"]: row for row in rows}
        self.assertEqual(set(columns), {"stage", "progress_percent", "updated_at"})
        self.assertEqual(columns["stage"]["COLUMN_DEFAULT"], "uploaded")
        self.assertEqual(str(columns["progress_percent"]["COLUMN_DEFAULT"]), "0")
        self.assertEqual(columns["updated_at"]["IS_NULLABLE"], "NO")

    def test_numeric_primary_keys_and_references_have_expected_types(self) -> None:
        expected = {
            ("chats", "id"),
            ("chat_messages", "chat_id"),
            ("ingest_jobs", "id"),
            ("publish_jobs", "id"),
            ("publish_changes", "publish_job_id"),
        }
        with self.storage.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, EXTRA
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                      AND (TABLE_NAME, COLUMN_NAME) IN (
                          ('chats', 'id'), ('chat_messages', 'chat_id'),
                          ('ingest_jobs', 'id'), ('publish_jobs', 'id'),
                          ('publish_changes', 'publish_job_id')
                      )
                    """,
                    (settings.mysql_database,),
                )
                rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT REFERENCED_TABLE_NAME
                    FROM information_schema.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME = 'chat_messages'
                      AND COLUMN_NAME = 'chat_id'
                      AND REFERENCED_TABLE_NAME = 'chats'
                    """,
                    (settings.mysql_database,),
                )
                foreign_key = cursor.fetchone()

        self.assertEqual({(row["TABLE_NAME"], row["COLUMN_NAME"]) for row in rows}, expected)
        self.assertTrue(all(row["COLUMN_TYPE"] == "bigint unsigned" for row in rows))
        self.assertTrue(
            all(
                row["EXTRA"] == "auto_increment"
                for row in rows
                if (row["TABLE_NAME"], row["COLUMN_NAME"])
                in {("chats", "id"), ("ingest_jobs", "id"), ("publish_jobs", "id")}
            )
        )
        self.assertIsNotNone(foreign_key)

    def test_ingest_progress_round_trip_with_real_mysql(self) -> None:
        created_at = datetime(2026, 7, 22, 10, 0, 0)

        failed_job = self.storage.create_ingest_job(
            status="queued",
            original_filename="failed.md",
            stored_filename="failed.md",
            source_path="raw/uploads/failed.md",
            created_at=created_at,
        )
        failed_job_id = failed_job.job_id
        self.created_ingest_job_ids.append(failed_job_id)
        self.assertIsInstance(failed_job_id, int)
        self.assertEqual((failed_job.stage, failed_job.progress_percent), ("uploaded", 0))
        self.assertEqual(failed_job.updated_at, created_at)

        started_at = datetime(2026, 7, 22, 10, 0, 1)
        writing_at = datetime(2026, 7, 22, 10, 0, 2)
        failed_at = datetime(2026, 7, 22, 10, 0, 3)
        self.storage.mark_ingest_job_running(failed_job_id, started_at)
        self.storage.update_ingest_job_progress(
            job_id=failed_job_id,
            stage="writing_wiki",
            progress_percent=65,
            updated_at=writing_at,
        )
        self.storage.mark_ingest_job_failed(
            job_id=failed_job_id,
            error="test failure",
            finished_at=failed_at,
        )
        reloaded_failed = self.storage.get_ingest_job(failed_job_id)
        self.assertIsNotNone(reloaded_failed)
        assert reloaded_failed is not None
        self.assertEqual(reloaded_failed.status, "failed")
        self.assertEqual((reloaded_failed.stage, reloaded_failed.progress_percent), ("writing_wiki", 65))
        self.assertEqual(reloaded_failed.updated_at, failed_at)

        succeeded_job = self.storage.create_ingest_job(
            status="queued",
            original_filename="succeeded.md",
            stored_filename="succeeded.md",
            source_path="raw/uploads/succeeded.md",
            created_at=created_at,
        )
        succeeded_job_id = succeeded_job.job_id
        self.created_ingest_job_ids.append(succeeded_job_id)
        finished_at = datetime(2026, 7, 22, 10, 0, 4)
        self.storage.mark_ingest_job_succeeded(
            job_id=succeeded_job_id,
            created_pages=["sources/succeeded.md"],
            updated_pages=["index.md"],
            contradictions=[],
            validation=IngestValidation(),
            finished_at=finished_at,
        )
        reloaded_succeeded = self.storage.get_ingest_job(succeeded_job_id)
        self.assertIsNotNone(reloaded_succeeded)
        assert reloaded_succeeded is not None
        self.assertEqual(reloaded_succeeded.status, "succeeded")
        self.assertEqual((reloaded_succeeded.stage, reloaded_succeeded.progress_percent), ("completed", 100))
        self.assertEqual(reloaded_succeeded.created_pages, ["sources/succeeded.md"])


if __name__ == "__main__":
    unittest.main()
