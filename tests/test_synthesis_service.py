from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.schemas.chat import ChatMessageResponse, ChatResponse
from app.services.chat_service import ChatService
from app.services.synthesis_service import (
    InvalidSynthesisMessageError,
    SynthesisAlreadyExistsError,
    SynthesisQuestionNotFoundError,
    SynthesisService,
)
from app.storage.mysql import ChatNotFoundError


class FakeStorage:
    def __init__(self) -> None:
        self.chat = ChatResponse(
            id=1,
            title="测试会话",
            status="active",
            created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            updated_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            last_message_at=None,
            last_message_preview=None,
        )
        self.messages: list[ChatMessageResponse] = []

    def get_chat(self, chat_id: int) -> ChatResponse | None:
        return self.chat if chat_id == self.chat.id else None

    def get_message(self, chat_id: int, message_id: int) -> ChatMessageResponse | None:
        for message in self.messages:
            if message.chat_id == chat_id and message.id == message_id:
                return message
        return None

    def get_previous_user_message(
        self,
        chat_id: int,
        before_message_id: int,
    ) -> ChatMessageResponse | None:
        previous = [
            message
            for message in self.messages
            if message.chat_id == chat_id and message.role == "user" and message.id < before_message_id
        ]
        return previous[-1] if previous else None

    def mark_message_synthesized(
        self,
        chat_id: int,
        message_id: int,
        synthesis_path: str,
        synthesized_at: datetime,
    ) -> ChatMessageResponse | None:
        message = self.get_message(chat_id, message_id)
        if message is None or message.role != "assistant" or message.synthesis_path is not None:
            return None
        updated = message.model_copy(
            update={
                "synthesis_path": synthesis_path,
                "synthesized_at": synthesized_at,
            }
        )
        self.messages = [updated if item.id == message_id else item for item in self.messages]
        return updated


class SynthesisServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        wiki_path = self.repo_path / "wiki"
        wiki_path.mkdir()
        (wiki_path / "index.md").write_text("# Wiki\n\n## Syntheses\n\n", encoding="utf-8")
        (wiki_path / "log.md").write_text("# Log\n\n", encoding="utf-8")
        self.storage = FakeStorage()
        self.chat_service = ChatService(self.storage)  # type: ignore[arg-type]
        self.service = SynthesisService(self.chat_service, self.repo_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_save_chat_answer_writes_synthesis_and_marks_message(self) -> None:
        self.storage.messages = [
            ChatMessageResponse(
                id=1,
                chat_id=1,
                role="user",
                content="MySQL 多轮聊天实现？",
                created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            ),
            ChatMessageResponse(
                id=2,
                chat_id=1,
                role="assistant",
                content="## 答案\n\n保留 [[PageA]]。",
                sources=["PageA"],
                relevant_pages=["concepts/page-a.md"],
                created_at=datetime(2026, 6, 22, 0, 0, 1, tzinfo=timezone.utc),
            ),
        ]

        response = self.service.save_chat_answer(
            chat_id=1,
            assistant_message_id=2,
            title=None,
        )

        self.assertEqual(response.question_message_id, 1)
        self.assertEqual(response.title, "MySQL 多轮聊天实现？")
        self.assertEqual(response.path, "syntheses/mysql-多轮聊天实现.md")
        saved_path = self.repo_path / "wiki" / response.path
        saved_markdown = saved_path.read_text(encoding="utf-8")
        self.assertIn('title: "MySQL 多轮聊天实现？"', saved_markdown)
        self.assertIn('  - "PageA"', saved_markdown)
        self.assertTrue(saved_markdown.endswith("## 答案\n\n保留 [[PageA]]。\n"))
        self.assertIn("[MySQL 多轮聊天实现？](syntheses/mysql-多轮聊天实现.md)", (self.repo_path / "wiki" / "index.md").read_text(encoding="utf-8"))
        self.assertEqual(self.storage.messages[1].synthesis_path, response.path)

    def test_save_uses_explicit_title_and_conflict_suffix(self) -> None:
        syntheses_dir = self.repo_path / "wiki" / "syntheses"
        syntheses_dir.mkdir()
        (syntheses_dir / "custom-title.md").write_text("existing", encoding="utf-8")
        self.storage.messages = [
            ChatMessageResponse(
                id=1,
                chat_id=1,
                role="user",
                content="question",
                created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            ),
            ChatMessageResponse(
                id=2,
                chat_id=1,
                role="assistant",
                content="answer",
                created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            ),
        ]

        response = self.service.save_chat_answer(
            chat_id=1,
            assistant_message_id=2,
            title="Custom Title",
        )

        self.assertEqual(response.title, "Custom Title")
        self.assertEqual(response.path, "syntheses/custom-title-2.md")

    def test_rejects_user_message(self) -> None:
        self.storage.messages = [
            ChatMessageResponse(
                id=1,
                chat_id=1,
                role="user",
                content="question",
                created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            )
        ]

        with self.assertRaises(InvalidSynthesisMessageError):
            self.service.save_chat_answer(chat_id=1, assistant_message_id=1, title=None)

    def test_rejects_missing_previous_question(self) -> None:
        self.storage.messages = [
            ChatMessageResponse(
                id=2,
                chat_id=1,
                role="assistant",
                content="answer",
                created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            )
        ]

        with self.assertRaises(SynthesisQuestionNotFoundError):
            self.service.save_chat_answer(chat_id=1, assistant_message_id=2, title=None)

    def test_rejects_duplicate_save(self) -> None:
        self.storage.messages = [
            ChatMessageResponse(
                id=1,
                chat_id=1,
                role="user",
                content="question",
                created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            ),
            ChatMessageResponse(
                id=2,
                chat_id=1,
                role="assistant",
                content="answer",
                created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
                synthesis_path="syntheses/existing.md",
            ),
        ]

        with self.assertRaises(SynthesisAlreadyExistsError) as context:
            self.service.save_chat_answer(chat_id=1, assistant_message_id=2, title=None)

        self.assertEqual(context.exception.path, "syntheses/existing.md")

    def test_missing_chat_raises_chat_not_found(self) -> None:
        with self.assertRaises(ChatNotFoundError):
            self.service.save_chat_answer(chat_id=2, assistant_message_id=2, title=None)


if __name__ == "__main__":
    unittest.main()
