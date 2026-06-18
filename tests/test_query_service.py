from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from app.schemas.chat import ChatMessageResponse
from app.schemas.query import QueryResult
from app.services.query_service import QueryService


class QueryServiceTests(unittest.TestCase):
    def _message(self, message_id: int, content: str) -> ChatMessageResponse:
        return ChatMessageResponse(
            id=message_id,
            chat_id="chat-1",
            role="user" if message_id % 2 else "assistant",
            content=content,
            created_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
        )

    def test_answer_prompt_contains_required_sections(self) -> None:
        prompt = QueryService._build_answer_prompt(
            question="current question",
            schema="schema text",
            pages_context="page context",
            conversation_history="User: previous question",
        )

        self.assertIn("Conversation history:", prompt)
        self.assertIn("Relevant wiki pages:", prompt)
        self.assertIn("Current user question:", prompt)
        self.assertIn("grounded in the wiki pages", prompt)
        self.assertIn("Do not repeat, quote, or paraphrase the current user question", prompt)
        self.assertIn("Do not use the current user question as the answer title or as a heading", prompt)
        self.assertIn("headings must be on their own line", prompt)
        self.assertIn("Never collapse headings, paragraphs, or bullet lists", prompt)

    def test_run_chat_turn_uses_latest_six_messages(self) -> None:
        service = QueryService.__new__(QueryService)
        captured_history: list[ChatMessageResponse] = []

        def fake_run(question: str, history_messages: list[ChatMessageResponse]) -> QueryResult:
            captured_history.extend(history_messages)
            return QueryResult(answer="answer", sources=[], relevant_pages=[])

        service._run = fake_run  # type: ignore[method-assign]
        history = [self._message(index, f"message-{index}") for index in range(8)]

        result = service.run_chat_turn("question", history)

        self.assertEqual(result.answer, "answer")
        self.assertEqual([message.content for message in captured_history], [f"message-{index}" for index in range(2, 8)])

    @patch("app.services.query_service.time.sleep")
    def test_llm_call_retries_once_after_transient_failure(self, sleep: Mock) -> None:
        call_count = 0

        def flaky_caller(prompt: str, max_tokens: int | None = None) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("connection reset")
            return "answer"

        answer = QueryService._call_llm_with_retry(
            flaky_caller,
            "prompt",
            max_tokens=512,
            operation="test",
        )

        self.assertEqual(answer, "answer")
        self.assertEqual(call_count, 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
