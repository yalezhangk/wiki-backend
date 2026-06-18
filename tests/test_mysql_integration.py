from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient

from app.config import settings
from app.main import create_app
from app.schemas.chat import ChatMessageResponse
from app.schemas.query import QueryResult
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
            answer=f"数据库联调回答：{question}",
            sources=["MySQL联调来源"],
            relevant_pages=["integration/mysql.md"],
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
        self.created_chat_ids: list[str] = []

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
        if not self.created_chat_ids:
            return
        placeholders = ", ".join(["%s"] * len(self.created_chat_ids))
        with self.storage.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM chats WHERE id IN ({placeholders})",
                    tuple(self.created_chat_ids),
                )

    def test_chat_api_round_trip_with_real_mysql(self) -> None:
        first_response = self.client.post("/api/chats")
        self.assertEqual(first_response.status_code, 200)
        first_chat = first_response.json()
        self.created_chat_ids.append(first_chat["id"])
        self.assertEqual(first_chat["title"], "新对话")

        second_response = self.client.post("/api/chats", json={"title": "待重命名"})
        self.assertEqual(second_response.status_code, 200)
        second_chat = second_response.json()
        self.created_chat_ids.append(second_chat["id"])

        turn_response = self.client.post(
            f"/api/chats/{first_chat['id']}/messages",
            json={"content": "MySQL 能保存多轮会话吗？"},
        )
        self.assertEqual(turn_response.status_code, 200)
        turn = turn_response.json()
        self.assertEqual(turn["chat"]["title"], "MySQL 能保存多轮会话吗？")
        self.assertEqual(turn["assistant_message"]["sources"], ["MySQL联调来源"])
        self.assertEqual(
            turn["assistant_message"]["relevant_pages"],
            ["integration/mysql.md"],
        )

        messages_response = self.client.get(f"/api/chats/{first_chat['id']}/messages")
        self.assertEqual(messages_response.status_code, 200)
        messages = messages_response.json()["messages"]
        self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["sources"], ["MySQL联调来源"])

        rename_response = self.client.patch(
            f"/api/chats/{second_chat['id']}",
            json={"title": "已完成重命名"},
        )
        self.assertEqual(rename_response.status_code, 200)
        self.assertEqual(rename_response.json()["title"], "已完成重命名")

        chats_response = self.client.get("/api/chats")
        self.assertEqual(chats_response.status_code, 200)
        created_ids = [
            chat["id"]
            for chat in chats_response.json()
            if chat["id"] in self.created_chat_ids
        ]
        self.assertEqual(created_ids, [second_chat["id"], first_chat["id"]])

    def test_tables_and_columns_have_comments(self) -> None:
        expected_column_counts = {"chats": 6, "chat_messages": 7}
        with self.storage.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT TABLE_NAME, TABLE_COMMENT
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME IN ('chats', 'chat_messages')
                    """,
                    (settings.mysql_database,),
                )
                table_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_COMMENT
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME IN ('chats', 'chat_messages')
                    """,
                    (settings.mysql_database,),
                )
                column_rows = cursor.fetchall()

        self.assertEqual(len(table_rows), 2)
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
        }
        with self.storage.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_NAME, DATETIME_PRECISION
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME IN ('chats', 'chat_messages')
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


if __name__ == "__main__":
    unittest.main()
