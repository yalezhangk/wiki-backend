from __future__ import annotations

import unittest
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.chat import ChatMessageResponse, ChatResponse
from app.schemas.query import CitationResponse, QueryResult
from app.services.query_service import QueryServiceError
from app.storage.mysql import ChatNotFoundError


class FakeChatService:
    def __init__(self) -> None:
        self.chat = ChatResponse(
            id=1,
            title="新对话",
            status="active",
            created_at=datetime(2026, 6, 17),
            updated_at=datetime(2026, 6, 17),
            last_message_at=None,
            last_message_preview=None,
        )
        self.messages: list[ChatMessageResponse] = []

    def list_chats(self) -> list[ChatResponse]:
        return [self.chat]

    def create_chat(self, title: str | None = None) -> ChatResponse:
        if title:
            self.chat = self.chat.model_copy(update={"title": title})
        return self.chat

    def get_chat(self, chat_id: int) -> ChatResponse:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        return self.chat

    def rename_chat(self, chat_id: int, title: str) -> ChatResponse:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        self.chat = self.chat.model_copy(update={"title": title})
        return self.chat

    def list_messages(self, chat_id: int) -> list[ChatMessageResponse]:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        return list(self.messages)


class FakeChatTurnService:
    def __init__(self, chat_service: FakeChatService) -> None:
        self._chat_service = chat_service
        self.should_fail = False

    def run_turn(self, chat_id: int, content: str):  # type: ignore[no-untyped-def]
        if chat_id != self._chat_service.chat.id:
            raise ChatNotFoundError(chat_id)
        if self.should_fail:
            raise QueryServiceError("llm failed")
        user_message = ChatMessageResponse(
            id=1,
            chat_id=chat_id,
            role="user",
            content=content,
            created_at=datetime(2026, 6, 17, 0, 0, 1),
        )
        assistant_message = ChatMessageResponse(
            id=2,
            chat_id=chat_id,
            role="assistant",
            content="answer",
            sources=["PageA"],
            relevant_pages=["topic/page-a.md"],
            citations=[
                CitationResponse(
                    path="topic/page-a.md",
                    title="Page A",
                    kind="page",
                )
            ],
            created_at=datetime(2026, 6, 17, 0, 0, 2),
        )
        return {
            "chat": self._chat_service.chat,
            "user_message": user_message,
            "assistant_message": assistant_message,
        }


class FakeQueryService:
    def run(self, question: str) -> QueryResult:
        return QueryResult(
            answer=f"answer:{question}",
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


class ChatsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chat_service = FakeChatService()
        self.chat_turn_service = FakeChatTurnService(self.chat_service)
        self.app = create_app(
            chat_service=self.chat_service,  # type: ignore[arg-type]
            chat_turn_service=self.chat_turn_service,  # type: ignore[arg-type]
            query_service=FakeQueryService(),  # type: ignore[arg-type]
            initialize_storage=False,
        )
        self.client = TestClient(self.app)

    def test_get_chats_returns_list(self) -> None:
        response = self.client.get("/api/chats")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()[0],
            {
                "id": 1,
                "title": "新对话",
                "status": "active",
                "created_at": "2026-06-17T00:00:00",
                "updated_at": "2026-06-17T00:00:00",
                "last_message_at": None,
                "last_message_preview": None,
            },
        )

    def test_create_chat_allows_empty_body(self) -> None:
        response = self.client.post("/api/chats")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "新对话")

    def test_get_messages_returns_chat_and_messages(self) -> None:
        markdown = "## 标题\n\n- 第一项\n  - 嵌套项"
        self.chat_service.messages.append(
            ChatMessageResponse(
                id=1,
                chat_id=1,
                role="assistant",
                content=markdown,
                created_at=datetime(2026, 6, 17),
            )
        )
        response = self.client.get("/api/chats/1/messages")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["chat"]["id"], 1)
        self.assertEqual(response.json()["messages"][0]["content"], markdown)
        self.assertEqual(response.json()["messages"][0]["sources"], [])
        self.assertEqual(response.json()["messages"][0]["relevant_pages"], [])
        self.assertEqual(response.json()["messages"][0]["citations"], [])
        self.assertEqual(response.json()["messages"][0]["created_at"], "2026-06-17T00:00:00")
        self.assertIsNone(response.json()["messages"][0]["synthesis_path"])
        self.assertIsNone(response.json()["messages"][0]["synthesized_at"])

    def test_post_message_returns_turn_payload(self) -> None:
        response = self.client.post("/api/chats/1/messages", json={"content": "hello"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["user_message"]["content"], "hello")
        self.assertEqual(payload["assistant_message"]["sources"], ["PageA"])
        self.assertEqual(payload["assistant_message"]["relevant_pages"], ["topic/page-a.md"])
        self.assertEqual(
            payload["assistant_message"]["citations"][0],
            {
                "path": "topic/page-a.md",
                "title": "Page A",
                "kind": "page",
                "excerpt": None,
                "relevance": None,
            },
        )

    def test_post_message_returns_404_for_missing_chat(self) -> None:
        response = self.client.post("/api/chats/2/messages", json={"content": "hello"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "chat not found")

    def test_post_message_returns_422_for_empty_content(self) -> None:
        response = self.client.post("/api/chats/1/messages", json={"content": "   "})

        self.assertEqual(response.status_code, 422)

    def test_post_message_returns_502_for_query_failure(self) -> None:
        self.chat_turn_service.should_fail = True

        response = self.client.post("/api/chats/1/messages", json={"content": "hello"})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "llm failed")

    def test_post_message_logs_502_query_failure(self) -> None:
        self.chat_turn_service.should_fail = True

        with self.assertLogs("uvicorn.error", level="ERROR") as logs:
            response = self.client.post("/api/chats/1/messages", json={"content": "hello"})

        self.assertEqual(response.status_code, 502)
        self.assertIn(
            "HTTP 502 for POST /api/chats/1/messages: llm failed",
            "\n".join(logs.output),
        )
        self.assertIsNotNone(logs.records[0].exc_info)

    def test_chat_routes_reject_uuid_and_non_positive_ids(self) -> None:
        self.assertEqual(self.client.get("/api/chats/chat-1/messages").status_code, 422)
        self.assertEqual(self.client.get("/api/chats/0/messages").status_code, 422)


if __name__ == "__main__":
    unittest.main()
