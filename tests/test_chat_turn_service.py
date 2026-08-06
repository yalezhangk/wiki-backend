from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.schemas.chat import ChatMessageResponse, ChatResponse
from app.schemas.query import CitationResponse, QueryResult
from app.model_profiles import ModelProfileService, ModelProfileUnavailableError
from app.services.chat_service import ChatService
from app.services.chat_turn_service import ChatTurnService
from app.services.query_service import QueryServiceError
from app.storage.mysql import ChatNotFoundError, MySQLStorage


class FakeSchemaCursor:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, query: str) -> None:
        self.queries.append(" ".join(query.split()))

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[dict[str, str]]:
        return []


class FakeStorage:
    def __init__(self) -> None:
        self.chat = ChatResponse(
            id=1,
            title="新对话",
            status="active",
            created_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
            updated_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
            last_message_at=None,
            last_message_preview=None,
        )
        self.messages: list[ChatMessageResponse] = []
        self.next_message_id = 1

    def list_chats(self) -> list[ChatResponse]:
        return [self.chat]

    def create_chat(self, title: str) -> ChatResponse:
        self.chat = self.chat.model_copy(update={"title": title})
        return self.chat

    def get_chat(self, chat_id: int) -> ChatResponse | None:
        return self.chat if chat_id == self.chat.id else None

    def rename_chat(self, chat_id: int, title: str) -> ChatResponse:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        self.chat = self.chat.model_copy(update={"title": title})
        return self.chat

    def list_messages(self, chat_id: int) -> list[ChatMessageResponse]:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        return list(self.messages)

    def list_recent_messages(
        self,
        chat_id: int,
        limit: int,
        before_message_id: int | None = None,
    ) -> list[ChatMessageResponse]:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        filtered = self.messages
        if before_message_id is not None:
            filtered = [message for message in filtered if message.id < before_message_id]
        return filtered[-limit:]

    def count_messages(self, chat_id: int) -> int:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        return len(self.messages)

    def create_message(
        self,
        chat_id: int,
        role: str,
        content: str,
        sources: list[str] | None = None,
        relevant_pages: list[str] | None = None,
        citations: list[CitationResponse] | None = None,
        model_profile_id: str | None = None,
        model_profile_label: str | None = None,
    ) -> ChatMessageResponse:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        created_at = datetime(2026, 6, 17, tzinfo=timezone.utc) + timedelta(seconds=self.next_message_id)
        message = ChatMessageResponse(
            id=self.next_message_id,
            chat_id=chat_id,
            role=role,
            content=content,
            sources=sources or [],
            relevant_pages=relevant_pages or [],
            citations=citations or [],
            model_profile_id=model_profile_id,
            model_profile_label=model_profile_label,
            created_at=created_at,
        )
        self.next_message_id += 1
        self.messages.append(message)
        return message

    def update_chat_activity(
        self,
        chat_id: int,
        updated_at: datetime,
        last_message_at: datetime | None,
    ) -> ChatResponse:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        last_preview = self.messages[-1].content if self.messages else None
        self.chat = self.chat.model_copy(
            update={
                "updated_at": updated_at,
                "last_message_at": last_message_at,
                "last_message_preview": last_preview,
            }
        )
        return self.chat


class FakeQueryService:
    def __init__(self) -> None:
        self.last_question: str | None = None
        self.last_history: list[ChatMessageResponse] = []
        self.should_fail = False
        self.answer = "Answer with [[PageA]]"

    def run_chat_turn(
        self,
        question: str,
        history_messages: list[ChatMessageResponse],
        model_profile: object | None = None,
    ) -> QueryResult:
        if self.should_fail:
            raise QueryServiceError("query failed")
        self.last_question = question
        self.last_history = list(history_messages)
        return QueryResult(
            answer=self.answer,
            sources=["PageA"],
            relevant_pages=["topic/page-a.md"],
            citations=[
                CitationResponse(
                    path="topic/page-a.md",
                    title="Page A",
                    kind="page",
                )
            ],
        )


class ChatTurnServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = FakeStorage()
        self.chat_service = ChatService(self.storage)  # type: ignore[arg-type]
        self.query_service = FakeQueryService()
        self.model_profile_service = ModelProfileService(availability_checker=lambda profile: True)
        self.service = ChatTurnService(
            chat_service=self.chat_service,
            query_service=self.query_service,  # type: ignore[arg-type]
            history_limit=6,
            model_profile_service=self.model_profile_service,
        )

    def test_first_turn_saves_messages_and_auto_renames_chat(self) -> None:
        response = self.service.run_turn(1, "这是第一条问题，需要自动命名标题", "deepseek-v4-flash")

        self.assertEqual(response.user_message.role, "user")
        self.assertEqual(response.assistant_message.role, "assistant")
        self.assertEqual(response.assistant_message.sources, ["PageA"])
        self.assertEqual(response.assistant_message.citations[0].path, "topic/page-a.md")
        self.assertEqual(response.chat.title, "这是第一条问题，需要自动命名标题")

    def test_turn_logs_selected_profile_without_message_content(self) -> None:
        question = "不应写入日志的私密问题"

        with self.assertLogs("app.services.chat_turn_service", level="INFO") as captured:
            self.service.run_turn(1, question, "local-qwen3.6-35b-direct")

        records = "\n".join(captured.output)
        self.assertIn("Chat turn started chat_id=1", records)
        self.assertIn("model_profile_id=local-qwen3.6-35b-direct", records)
        self.assertIn("model=qwen3.6:35b", records)
        self.assertIn("reasoning_mode=direct", records)
        self.assertIn("reasoning_effort=none", records)
        self.assertIn("Chat user message persisted chat_id=1 user_message_id=1", records)
        self.assertIn("Chat turn completed chat_id=1 user_message_id=1 assistant_message_id=2", records)
        self.assertNotIn(question, records)

    def test_turn_preserves_multiline_markdown_and_normalizes_title(self) -> None:
        question = "  第一行\n  第二行  "
        markdown = """  ## 标题

- 第一项
  - 嵌套项

| 列一 | 列二 |
| --- | --- |
| A | B |

> 引用

```python
value = 1
```  """
        self.query_service.answer = markdown

        response = self.service.run_turn(1, question, "deepseek-v4-flash")

        self.assertEqual(response.user_message.content, "第一行\n  第二行")
        self.assertEqual(response.assistant_message.content, markdown.strip())
        self.assertEqual(response.assistant_message.content, self.query_service.answer.strip())
        self.assertEqual(response.chat.title, "第一行 第二行")

    def test_second_turn_uses_recent_history_only(self) -> None:
        for index in range(8):
            role = "user" if index % 2 == 0 else "assistant"
            self.storage.create_message(1, role=role, content=f"message-{index}")

        self.service.run_turn(1, "follow-up question", "deepseek-v4-flash")

        self.assertEqual(self.query_service.last_question, "follow-up question")
        self.assertEqual(len(self.query_service.last_history), 6)
        self.assertEqual(
            [message.content for message in self.query_service.last_history],
            [f"message-{index}" for index in range(2, 8)],
        )

    def test_query_failure_keeps_user_message_without_assistant_message(self) -> None:
        self.query_service.should_fail = True

        with self.assertRaises(QueryServiceError):
            self.service.run_turn(1, "will fail", "deepseek-v4-flash")

        self.assertEqual(len(self.storage.messages), 1)
        self.assertEqual(self.storage.messages[0].role, "user")

    def test_unavailable_profile_does_not_write_user_message(self) -> None:
        unavailable_service = ChatTurnService(
            chat_service=self.chat_service,
            query_service=self.query_service,  # type: ignore[arg-type]
            history_limit=6,
            model_profile_service=ModelProfileService(availability_checker=lambda profile: False),
        )

        with self.assertRaises(ModelProfileUnavailableError):
            unavailable_service.run_turn(1, "will not persist", "deepseek-v4-flash")

        self.assertEqual(self.storage.messages, [])

    def test_mysql_upgrade_backfills_historical_citations(self) -> None:
        cursor = FakeSchemaCursor()

        MySQLStorage._ensure_message_citations_column(cursor)

        statements = "\n".join(cursor.queries)
        self.assertIn("ADD COLUMN citations JSON NULL", statements)
        self.assertIn("SET citations = JSON_ARRAY()", statements)
        self.assertIn("MODIFY COLUMN citations JSON NOT NULL", statements)

    def test_mysql_upgrade_adds_nullable_model_profile_snapshot_columns(self) -> None:
        cursor = FakeSchemaCursor()

        MySQLStorage._ensure_message_model_profile_columns(cursor)

        statements = "\n".join(cursor.queries)
        self.assertIn("ADD COLUMN model_profile_id VARCHAR(100) NULL", statements)
        self.assertIn("ADD COLUMN model_profile_label VARCHAR(200) NULL", statements)

    def test_historical_message_without_citations_recovers_empty_list(self) -> None:
        storage = MySQLStorage("127.0.0.1", 3306, "user", "password", "database")
        message = storage._message_from_row(
            {
                "id": 1,
                "chat_id": 1,
                "role": "assistant",
                "content": "answer",
                "sources": "[]",
                "relevant_pages": "[]",
                "created_at": datetime(2026, 7, 22),
            }
        )

        self.assertEqual(message.citations, [])
        self.assertIsNone(message.model_profile_id)
        self.assertIsNone(message.model_profile_label)


if __name__ == "__main__":
    unittest.main()
