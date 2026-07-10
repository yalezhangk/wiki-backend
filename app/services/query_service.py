from __future__ import annotations

import importlib
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from app.schemas.chat import ChatMessageResponse
from app.schemas.query import QueryResult

LOGGER = logging.getLogger(__name__)
SOURCE_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")


class QueryServiceError(RuntimeError):
    """Raised when the backend cannot complete a wiki query."""


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def find_relevant_pages(question: str, index_content: str, wiki_dir: Path, graph_json: Path) -> list[Path]:
    md_links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", index_content)
    question_lower = question.lower()
    relevant: list[Path] = []

    for title, href in md_links:
        title_lower = title.lower()
        has_cjk = any("\u4e00" <= char <= "\u9fff" for char in title)
        if has_cjk:
            matched = any(
                title_lower[index : index + 2] in question_lower
                for index in range(len(title_lower) - 1)
                if any("\u4e00" <= char <= "\u9fff" for char in title_lower[index : index + 2])
            )
        else:
            matched = any(word in question_lower for word in title_lower.split() if len(word) > 2)

        if matched:
            page = wiki_dir / href
            if page.exists() and page not in relevant:
                relevant.append(page)

    if graph_json.exists() and relevant:
        try:
            graph_data = json.loads(graph_json.read_text(encoding="utf-8"))
            page_ids = {page.relative_to(wiki_dir).as_posix().replace(".md", "") for page in relevant}
            neighbors: set[str] = set()
            for edge in graph_data.get("edges", []):
                if edge.get("confidence", 0) >= 0.7:
                    if edge.get("from") in page_ids:
                        neighbors.add(edge["to"])
                    elif edge.get("to") in page_ids:
                        neighbors.add(edge["from"])
            for node_id in neighbors:
                neighbor = wiki_dir / f"{node_id}.md"
                if neighbor.exists() and neighbor not in relevant:
                    relevant.append(neighbor)
        except (json.JSONDecodeError, KeyError, TypeError):
            LOGGER.warning("Failed to expand relevant pages from graph.json", exc_info=True)

    overview = wiki_dir / "overview.md"
    if overview.exists() and overview not in relevant:
        relevant.insert(0, overview)
    return relevant[:15]


class QueryService:
    def __init__(self, agent_root: Path) -> None:
        self._agent_root = agent_root.resolve()
        self._wiki_dir = self._agent_root / "wiki"
        self._index_file = self._wiki_dir / "index.md"
        self._schema_file = self._agent_root / "AGENTS.md"
        self._graph_json = self._agent_root / "graph" / "graph.json"
        self._call_llm_fast: Callable[[str, int | None], str] | None = None
        self._call_llm_main: Callable[[str, int | None], str] | None = None

    def run(self, question: str) -> QueryResult:
        return self._run(question=question, history_messages=[])

    def run_chat_turn(self, question: str, history_messages: Sequence[ChatMessageResponse]) -> QueryResult:
        return self._run(question=question, history_messages=list(history_messages)[-6:])

    def _run(self, question: str, history_messages: Sequence[ChatMessageResponse]) -> QueryResult:
        normalized_question = question.strip()
        if not normalized_question:
            raise QueryServiceError("question cannot be empty")

        index_content = read_file(self._index_file)
        if not index_content:
            raise QueryServiceError("Wiki is empty. Ingest some sources first before querying.")

        relevant_pages = self._select_relevant_pages(normalized_question, index_content)
        pages_context = self._build_pages_context(relevant_pages, index_content)
        schema = read_file(self._schema_file)
        prompt = self._build_answer_prompt(
            question=normalized_question,
            schema=schema,
            pages_context=pages_context,
            conversation_history=self._build_conversation_history(history_messages),
        )
        _, call_llm_main = self._get_llm_callers()

        LOGGER.info("Running wiki query question=%r relevant_pages=%d", normalized_question, len(relevant_pages))

        try:
            answer = self._call_llm_with_retry(
                call_llm_main,
                prompt,
                max_tokens=4096,
                operation="answer generation",
            )
        except Exception as exc:
            raise QueryServiceError("Failed to generate query answer via llm-wiki-agent LLM config.") from exc

        normalized_answer = answer.strip()
        if not normalized_answer:
            raise QueryServiceError("llm-wiki-agent returned an empty answer")

        return QueryResult(
            answer=normalized_answer,
            sources=sorted(set(SOURCE_PATTERN.findall(normalized_answer))),
            relevant_pages=[page.relative_to(self._wiki_dir).as_posix() for page in relevant_pages],
        )

    def _select_relevant_pages(self, question: str, index_content: str) -> list[Path]:
        relevant_pages = find_relevant_pages(question, index_content, self._wiki_dir, self._graph_json)
        if relevant_pages and len(relevant_pages) > 1:
            return relevant_pages

        LOGGER.info("Falling back to model-based page selection")
        call_llm_fast, _ = self._get_llm_callers()
        prompt = (
            "Given this wiki index:\n\n"
            f"{index_content}\n\n"
            f'Which pages are most relevant to answering: "{question}"\n\n'
            'Return ONLY a JSON array of relative file paths (as listed in the index), '
            'e.g. ["sources/foo.md", "concepts/Bar.md"]. Maximum 10 pages.'
        )
        try:
            raw = self._call_llm_with_retry(
                call_llm_fast,
                prompt,
                max_tokens=512,
                operation="page selection",
            )
        except Exception:
            LOGGER.warning("Model page selection failed", exc_info=True)
            return relevant_pages

        parsed_paths = self._parse_json_array(raw)
        if parsed_paths is None:
            return relevant_pages

        selected_pages: list[Path] = []
        for path_text in parsed_paths:
            if not isinstance(path_text, str):
                continue
            candidate = self._wiki_dir / path_text
            if candidate.exists() and candidate not in selected_pages:
                selected_pages.append(candidate)

        return selected_pages or relevant_pages

    @staticmethod
    def _call_llm_with_retry(
        caller: Callable[[str, int | None], str],
        prompt: str,
        *,
        max_tokens: int,
        operation: str,
    ) -> str:
        for attempt in range(2):
            try:
                return caller(prompt, max_tokens=max_tokens)
            except Exception as exc:
                if attempt == 1:
                    raise
                LOGGER.warning("LLM %s failed; retrying once: %s", operation, exc)
                time.sleep(1)
        raise RuntimeError("unreachable LLM retry state")

    def _build_pages_context(self, pages: list[Path], index_content: str) -> str:
        if not pages:
            return f"\n\n### wiki/index.md\n{index_content}"

        context_parts: list[str] = []
        for page in pages:
            relative_path = page.relative_to(self._agent_root).as_posix()
            context_parts.append(f"\n\n### {relative_path}\n{page.read_text(encoding='utf-8')}")
        return "".join(context_parts)

    @staticmethod
    def _build_conversation_history(history_messages: Sequence[ChatMessageResponse]) -> str:
        if not history_messages:
            return "(none)"

        lines: list[str] = []
        for message in history_messages:
            role_label = "User" if message.role == "user" else "Assistant"
            lines.append(f"{role_label}: {message.content}")
        return "\n".join(lines)

    @staticmethod
    def _build_answer_prompt(
        question: str,
        schema: str,
        pages_context: str,
        conversation_history: str,
    ) -> str:
        return f"""You are querying an LLM Wiki to answer a question. Use the wiki pages below to synthesize a thorough answer.

Schema:
{schema}

Conversation history:
{conversation_history}

Relevant wiki pages:
{pages_context}

Current user question:
{question}

Requirements:
- Use the conversation history only to resolve context, references, and ellipsis.
- The final answer must still be grounded in the wiki pages above.
- Start with the answer itself. Do not repeat, quote, or paraphrase the current user question.
- Do not use the current user question as the answer title or as a heading.
- Cite sources using [[PageName]] wikilink syntax.
- Write a well-structured markdown answer. Use headers and bullets only when they improve readability.
- Preserve Markdown block structure: headings must be on their own line, paragraphs must be separated by a blank line, and each bullet must be on its own line.
- Never collapse headings, paragraphs, or bullet lists into a single line.
- At the end, add a ## Sources section listing the pages you drew from.
"""

    @staticmethod
    def _parse_json_array(raw: str) -> list[str] | None:
        sanitized = raw.strip()
        sanitized = re.sub(r"^```(?:json)?\s*", "", sanitized)
        sanitized = re.sub(r"\s*```$", "", sanitized)
        try:
            parsed = json.loads(sanitized)
        except json.JSONDecodeError:
            LOGGER.warning("Failed to decode page selector response as JSON")
            return None
        if not isinstance(parsed, list):
            return None
        return parsed

    def _get_llm_callers(self) -> tuple[Callable[[str, int | None], str], Callable[[str, int | None], str]]:
        if self._call_llm_fast is None or self._call_llm_main is None:
            self._call_llm_fast, self._call_llm_main = self._load_llm_callers()
        assert self._call_llm_fast is not None
        assert self._call_llm_main is not None
        return self._call_llm_fast, self._call_llm_main

    def _load_llm_callers(self) -> tuple[Callable[[str, int | None], str], Callable[[str, int | None], str]]:
        if not self._agent_root.exists():
            raise QueryServiceError(f"llm-wiki-agent repo not found: {self._agent_root}")

        agent_root_text = str(self._agent_root)
        if agent_root_text not in sys.path:
            sys.path.insert(0, agent_root_text)

        try:
            llm_config = importlib.import_module("tools.llm_config")
        except ModuleNotFoundError as exc:
            raise QueryServiceError("Unable to import llm-wiki-agent tools.llm_config.") from exc

        call_llm_fast = getattr(llm_config, "call_llm_fast", None)
        call_llm_main = getattr(llm_config, "call_llm_main", None)
        if not callable(call_llm_fast) or not callable(call_llm_main):
            raise QueryServiceError("llm-wiki-agent tools.llm_config is missing required callables.")

        return call_llm_fast, call_llm_main
