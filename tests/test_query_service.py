from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from app.config import settings
from app.schemas.chat import ChatMessageResponse
from app.schemas.query import CitationResponse, QueryResult
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
            sources=["entities/First.md", "sources/second.md"],
            citations=[],
            use_wiki_links=False,
        )

        self.assertIn("Conversation history:", prompt)
        self.assertIn("Relevant wiki pages:", prompt)
        self.assertIn("Current user question:", prompt)
        self.assertIn("grounded in the wiki pages", prompt)
        self.assertIn("Do not repeat, quote, or paraphrase the current user question", prompt)
        self.assertIn("Do not use the current user question as the answer title or as a heading", prompt)
        self.assertIn("headings must be on their own line", prompt)
        self.assertIn("Never collapse headings, paragraphs, or bullet lists", prompt)
        self.assertIn("override any conflicting citation instruction", prompt)
        self.assertIn("Do not add a `## Sources`", prompt)
        self.assertIn("[1] entities/First.md", prompt)
        self.assertIn("[2] sources/second.md", prompt)

    def test_chat_answer_prompt_uses_actual_wiki_link_references(self) -> None:
        citation = CitationResponse(
            path="entities/MDC4.md",
            title="MDC4 智能监控单元",
            kind="entity",
        )

        prompt = QueryService._build_answer_prompt(
            question="current question",
            schema="schema text",
            pages_context="page context",
            conversation_history="(none)",
            sources=["entities/MDC4.md"],
            citations=[citation],
            use_wiki_links=True,
        )

        self.assertIn("Do not use numbered `[n]` citations", prompt)
        self.assertIn("[[entities/MDC4|MDC4 智能监控单元]]", prompt)

    def test_chat_citations_convert_numeric_markers_to_wiki_links(self) -> None:
        citation = CitationResponse(
            path="entities/MDC4.md",
            title="MDC4 智能监控单元",
            kind="entity",
        )

        answer = QueryService._replace_inline_citations_with_wiki_links(
            "MDC4 是智能监控单元。[1]",
            [citation],
        )

        self.assertEqual(answer, "MDC4 是智能监控单元。[[entities/MDC4|MDC4 智能监控单元]]")

    def test_run_chat_turn_returns_actual_wiki_link_for_numeric_citation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wiki = root / "wiki"
            entities = wiki / "entities"
            entities.mkdir(parents=True)
            (wiki / "index.md").write_text("# Index", encoding="utf-8")
            page = entities / "MDC4.md"
            page.write_text(
                '---\ntitle: "MDC4 智能监控单元"\ntype: entity\n---\n\n# MDC4\n',
                encoding="utf-8",
            )
            service = QueryService(root)
            service._select_relevant_pages = lambda question, index: [page]  # type: ignore[method-assign]
            service._call_llm_fast = lambda prompt, max_tokens=None: "[]"
            service._call_llm_main = lambda prompt, max_tokens=None: "MDC4 是智能监控单元。[1]"

            result = service.run_chat_turn("MDC4 是什么？", [])

        self.assertEqual(result.answer, "MDC4 是智能监控单元。[[entities/MDC4|MDC4 智能监控单元]]")
        self.assertEqual(result.sources, ["entities/MDC4.md"])

    def test_run_chat_turn_uses_latest_six_messages(self) -> None:
        service = QueryService.__new__(QueryService)
        captured_history: list[ChatMessageResponse] = []

        def fake_run(
            question: str,
            history_messages: list[ChatMessageResponse],
            use_wiki_links: bool,
        ) -> QueryResult:
            self.assertTrue(use_wiki_links)
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

    def test_normalize_answer_removes_legacy_sources_and_invalid_markers(self) -> None:
        answer = (
            "Supported claim.[1] Unsupported claim.[3] No source.[0]\n\n"
            "## 引用来源\n"
            "- sources/first.md\n"
        )

        normalized = QueryService._normalize_answer(answer, source_count=2)

        self.assertEqual(normalized, "Supported claim.[1] Unsupported claim. No source.")
        self.assertEqual(
            QueryService._normalize_answer("Supported claim.[1]\n\n## Source", source_count=1),
            "Supported claim.[1]",
        )

    def test_run_uses_stable_source_paths_for_inline_marker_numbers(self) -> None:
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
                lambda prompt, max_tokens=None: (
                    "Second evidence.[1] First evidence.[2] Invalid marker.[3]\n\n"
                    "## Sources\n"
                    "- entities/Second.md\n"
                )
            )
            second = entities / "Second.md"
            first = entities / "First.md"
            service._select_relevant_pages = lambda question, index: [second, first, second]  # type: ignore[method-assign]

            result = service.run("question")

        self.assertEqual(result.answer, "Second evidence.[1] First evidence.[2] Invalid marker.")
        self.assertEqual(result.sources, ["entities/Second.md", "entities/First.md"])
        self.assertEqual(
            [citation.title for citation in result.citations],
            ["Second", "First"],
        )

    def test_run_uses_configured_main_model_token_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wiki = root / "wiki"
            wiki.mkdir()
            (wiki / "index.md").write_text("# Index", encoding="utf-8")
            service = QueryService(root)
            observed_max_tokens: list[int | None] = []
            service._select_relevant_pages = lambda question, index: []  # type: ignore[method-assign]
            service._call_llm_fast = lambda prompt, max_tokens=None: "[]"  # type: ignore[method-assign]
            service._call_llm_main = lambda prompt, max_tokens=None: (  # type: ignore[method-assign]
                observed_max_tokens.append(max_tokens) or "answer"
            )

            with patch.object(settings, "llm_main_max_tokens", 6144):
                service.run("question")

        self.assertEqual(observed_max_tokens, [6144])

    def test_page_selection_uses_configured_fast_model_token_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wiki = root / "wiki"
            wiki.mkdir()
            service = QueryService(root)
            observed_max_tokens: list[int | None] = []
            service._call_llm_fast = lambda prompt, max_tokens=None: (  # type: ignore[method-assign]
                observed_max_tokens.append(max_tokens) or "[]"
            )
            service._call_llm_main = lambda prompt, max_tokens=None: "answer"  # type: ignore[method-assign]

            with patch.object(settings, "llm_fast_max_tokens", 768):
                service._select_relevant_pages("question", "# Index")

        self.assertEqual(observed_max_tokens, [768])

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
