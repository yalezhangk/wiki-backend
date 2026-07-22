from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
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

    def test_build_citations_uses_frontmatter_for_chinese_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            page = root / "wiki" / "entities" / "智能监控单元.md"
            page.parent.mkdir(parents=True)
            page.write_text(
                '---\r\ntitle: "MDC4 智能监控单元"\r\ntype: entity\r\n---\r\n\r\n# 正文\r\n',
                encoding="utf-8",
                newline="",
            )
            service = QueryService(root)

            citations = service._build_citations(
                sources=["智能监控单元"],
                relevant_pages=[page],
            )

        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0].path, "entities/智能监控单元.md")
        self.assertEqual(citations[0].title, "MDC4 智能监控单元")
        self.assertEqual(citations[0].kind, "entity")
        self.assertIsNone(citations[0].excerpt)
        self.assertIsNone(citations[0].relevance)

    def test_build_citations_falls_back_for_missing_or_unknown_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            page = root / "wiki" / "misc" / "中文说明.md"
            page.parent.mkdir(parents=True)
            page.write_text("# 中文说明标题\n\n正文\n", encoding="utf-8")
            service = QueryService(root)

            citations = service._build_citations(sources=["中文说明"], relevant_pages=[page])

        self.assertEqual(citations[0].title, "中文说明标题")
        self.assertEqual(citations[0].kind, "page")

    def test_build_citations_maps_unknown_frontmatter_type_to_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            page = root / "wiki" / "misc" / "mystery.md"
            page.parent.mkdir(parents=True)
            page.write_text(
                '---\ntitle: "Mystery"\ntype: unexpected\n---\n',
                encoding="utf-8",
            )
            service = QueryService(root)

            citations = service._build_citations(sources=["mystery"], relevant_pages=[page])

        self.assertEqual(citations[0].title, "Mystery")
        self.assertEqual(citations[0].kind, "page")

    def test_build_citations_returns_empty_for_empty_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            page = root / "wiki" / "misc" / "context-only.md"
            page.parent.mkdir(parents=True)
            page.write_text("# Context only", encoding="utf-8")
            service = QueryService(root)

            self.assertEqual(service._build_citations(sources=[], relevant_pages=[page]), [])

    def test_build_citations_preserves_answer_reference_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wiki = root / "wiki" / "entities"
            wiki.mkdir(parents=True)
            first = wiki / "First.md"
            second = wiki / "Second.md"
            first.write_text("# First", encoding="utf-8")
            second.write_text("# Second", encoding="utf-8")
            service = QueryService(root)

            citations = service._build_citations(
                sources=["Second", "First", "Second"],
                relevant_pages=[first, second],
            )

        self.assertEqual([citation.title for citation in citations], ["Second", "First"])

    def test_run_keeps_legacy_sources_sorted_and_citations_in_answer_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wiki = root / "wiki"
            entities = wiki / "entities"
            entities.mkdir(parents=True)
            (wiki / "index.md").write_text("# Index", encoding="utf-8")
            (entities / "First.md").write_text("# First", encoding="utf-8")
            (entities / "Second.md").write_text("# Second", encoding="utf-8")
            service = QueryService(root)
            service._call_llm_fast = lambda prompt, max_tokens=None: "[]"
            service._call_llm_main = (
                lambda prompt, max_tokens=None: "[[Second]] then [[First]] and [[Second]]"
            )

            result = service.run("question")

        self.assertEqual(result.sources, ["First", "Second"])
        self.assertEqual(
            [citation.title for citation in result.citations],
            ["Second", "First"],
        )

    def test_resolve_wiki_page_rejects_absolute_and_parent_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wiki = root / "wiki"
            wiki.mkdir()
            outside = root / "secret.md"
            outside.write_text("secret", encoding="utf-8")
            service = QueryService(root)

            self.assertIsNone(service._resolve_wiki_page("../secret.md"))
            self.assertIsNone(service._resolve_wiki_page(str(outside.resolve())))


if __name__ == "__main__":
    unittest.main()
