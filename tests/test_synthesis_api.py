from __future__ import annotations

import unittest
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.synthesis import SynthesisResponse
from app.services.synthesis_service import (
    InvalidSynthesisMessageError,
    SynthesisAlreadyExistsError,
    SynthesisQuestionNotFoundError,
)
from app.storage.mysql import ChatNotFoundError, StorageUnavailableError


class FakeSynthesisService:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.last_payload: dict[str, object] | None = None

    def save_chat_answer(
        self,
        *,
        chat_id: str,
        assistant_message_id: int,
        title: str | None,
    ) -> SynthesisResponse:
        self.last_payload = {
            "chat_id": chat_id,
            "assistant_message_id": assistant_message_id,
            "title": title,
        }
        if self.error is not None:
            raise self.error
        return SynthesisResponse(
            chat_id=chat_id,
            assistant_message_id=assistant_message_id,
            question_message_id=assistant_message_id - 1,
            title=title or "自动标题",
            path="syntheses/auto.md",
            created_at=datetime(2026, 6, 22),
        )


class SynthesisApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeSynthesisService()
        self.client = TestClient(
            create_app(
                synthesis_service=self.service,  # type: ignore[arg-type]
                initialize_storage=False,
            )
        )

    def test_create_synthesis_posts_message_identity_only(self) -> None:
        response = self.client.post(
            "/api/synthesis",
            json={
                "chat_id": "chat-1",
                "assistant_message_id": 2,
                "title": "自定义标题",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "chat_id": "chat-1",
                "assistant_message_id": 2,
                "question_message_id": 1,
                "title": "自定义标题",
                "path": "syntheses/auto.md",
                "created_at": "2026-06-22T00:00:00",
            },
        )
        self.assertEqual(
            self.service.last_payload,
            {"chat_id": "chat-1", "assistant_message_id": 2, "title": "自定义标题"},
        )

    def test_create_synthesis_rejects_empty_title(self) -> None:
        response = self.client.post(
            "/api/synthesis",
            json={"chat_id": "chat-1", "assistant_message_id": 2, "title": "   "},
        )

        self.assertEqual(response.status_code, 422)

    def test_create_synthesis_maps_not_found(self) -> None:
        self.service.error = ChatNotFoundError("missing")

        response = self.client.post(
            "/api/synthesis",
            json={"chat_id": "missing", "assistant_message_id": 2},
        )

        self.assertEqual(response.status_code, 404)

    def test_create_synthesis_maps_invalid_message(self) -> None:
        self.service.error = InvalidSynthesisMessageError("message is not an assistant answer")

        response = self.client.post(
            "/api/synthesis",
            json={"chat_id": "chat-1", "assistant_message_id": 1},
        )

        self.assertEqual(response.status_code, 422)

    def test_create_synthesis_maps_duplicate_with_path(self) -> None:
        self.service.error = SynthesisAlreadyExistsError("already saved", "syntheses/existing.md")

        response = self.client.post(
            "/api/synthesis",
            json={"chat_id": "chat-1", "assistant_message_id": 2},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["path"], "syntheses/existing.md")

    def test_create_synthesis_maps_missing_question(self) -> None:
        self.service.error = SynthesisQuestionNotFoundError("previous user question not found")

        response = self.client.post(
            "/api/synthesis",
            json={"chat_id": "chat-1", "assistant_message_id": 2},
        )

        self.assertEqual(response.status_code, 409)

    def test_create_synthesis_maps_storage_unavailable(self) -> None:
        self.service.error = StorageUnavailableError("db unavailable")

        response = self.client.post(
            "/api/synthesis",
            json={"chat_id": "chat-1", "assistant_message_id": 2},
        )

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
