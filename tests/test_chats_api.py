from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.chat import ChatMessageResponse, ChatResponse
from app.schemas.query import QueryResult
from app.services.query_service import QueryServiceError
from app.storage.mysql import ChatNotFoundError


class FakeChatService:
    def __init__(self) -> None:
        self.chat = ChatResponse(
            id="chat-1",
            title="新对话",
            status="active",
            created_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
            updated_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
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

    def get_chat(self, chat_id: str) -> ChatResponse:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        return self.chat

    def rename_chat(self, chat_id: str, title: str) -> ChatResponse:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        self.chat = self.chat.model_copy(update={"title": title})
        return self.chat

    def list_messages(self, chat_id: str) -> list[ChatMessageResponse]:
        if chat_id != self.chat.id:
            raise ChatNotFoundError(chat_id)
        return list(self.messages)


class FakeChatTurnService:
    def __init__(self, chat_service: FakeChatService) -> None:
        self._chat_service = chat_service
        self.should_fail = False

    def run_turn(self, chat_id: str, content: str):  # type: ignore[no-untyped-def]
        if chat_id != self._chat_service.chat.id:
            raise ChatNotFoundError(chat_id)
        if self.should_fail:
            raise QueryServiceError("llm failed")
        user_message = ChatMessageResponse(
            id=1,
            chat_id=chat_id,
            role="user",
            content=content,
            created_at=datetime(2026, 6, 17, 0, 0, 1, tzinfo=timezone.utc),
        )
        assistant_message = ChatMessageResponse(
            id=2,
            chat_id=chat_id,
            role="assistant",
            content="answer",
            sources=["PageA"],
            relevant_pages=["topic/page-a.md"],
            created_at=datetime(2026, 6, 17, 0, 0, 2, tzinfo=timezone.utc),
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
        self.assertEqual(response.json()[0]["id"], "chat-1")

    def test_create_chat_allows_empty_body(self) -> None:
        response = self.client.post("/api/chats")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "新对话")

    def test_get_messages_returns_chat_and_messages(self) -> None:
        response = self.client.get("/api/chats/chat-1/messages")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["chat"]["id"], "chat-1")
        self.assertEqual(response.json()["messages"], [])

    def test_post_message_returns_turn_payload(self) -> None:
        response = self.client.post("/api/chats/chat-1/messages", json={"content": "hello"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["user_message"]["content"], "hello")
        self.assertEqual(payload["assistant_message"]["sources"], ["PageA"])

    def test_post_message_returns_404_for_missing_chat(self) -> None:
        response = self.client.post("/api/chats/missing/messages", json={"content": "hello"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "chat not found")

    def test_post_message_returns_422_for_empty_content(self) -> None:
        response = self.client.post("/api/chats/chat-1/messages", json={"content": "   "})

        self.assertEqual(response.status_code, 422)

    def test_post_message_returns_502_for_query_failure(self) -> None:
        self.chat_turn_service.should_fail = True

        response = self.client.post("/api/chats/chat-1/messages", json={"content": "hello"})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "llm failed")


if __name__ == "__main__":
    unittest.main()
