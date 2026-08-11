from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Sequence, cast

from app.config import settings
from app.llm_config import LLMProfile, call_llm_fast, call_llm_main, call_llm_profile
from app.prompts import load_prompt, render_prompt
from app.schemas.chat import ChatMessageResponse
from app.schemas.query import CitationKind, CitationResponse, QueryResult
from app.services.wiki_page_policy import is_knowledge_page

LOGGER = logging.getLogger(__name__)
INLINE_CITATION_PATTERN = re.compile(r"\[(\d+)\]")
TRAILING_SOURCES_SECTION_PATTERN = re.compile(
    r"(?:\n|^)[ \t]{0,3}#{2,6}[ \t]*(?:sources?|引用来源)[ \t]*:?[ \t]*(?:\n.*)?\Z",
    re.IGNORECASE | re.DOTALL,
)
FRONTMATTER_FIELD_PATTERN = re.compile(r"^(title|type):\s*(.*?)\s*$", re.MULTILINE)
HEADING_PATTERN = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
KNOWN_CITATION_KINDS = {"source", "entity", "concept", "synthesis", "page"}
DIRECTORY_KINDS = {
    "sources": "source",
    "entities": "entity",
    "concepts": "concept",
    "syntheses": "synthesis",
}
MAX_MODEL_SELECTED_PAGES = 10
MAX_DIRECT_HIGH_CONFIDENCE_PAGES = 5
PAGE_SELECTION_HISTORY_MESSAGE_LIMIT = 4
PAGE_SELECTION_HISTORY_MAX_CHARS = 1200
PAGE_SELECTION_HISTORY_MESSAGE_MAX_CHARS = 300


class QueryServiceError(RuntimeError):
    """Raised when the backend cannot complete a wiki query."""


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def resolve_wiki_page(wiki_dir: Path, value: str) -> Path | None:
    normalized = value.strip().replace("\\", "/")
    if not normalized:
        return None
    relative = Path(normalized)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    if relative.suffix == "":
        relative = relative.with_suffix(".md")
    if relative.suffix.lower() != ".md":
        return None
    wiki_root = wiki_dir.resolve()
    candidate = (wiki_root / relative).resolve()
    try:
        candidate.relative_to(wiki_root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def is_high_confidence_title_match(question: str, title: str) -> bool:
    """判断索引标题是否被问题完整提及，而不是仅命中宽泛关键词。"""
    normalized_question = " ".join(question.casefold().split())
    normalized_title = " ".join(title.casefold().split())
    if not normalized_title:
        return False

    if re.fullmatch(r"[a-z0-9][a-z0-9\s._+/-]*", normalized_title):
        escaped_title = re.escape(normalized_title).replace(r"\ ", r"\s+")
        return re.search(
            rf"(?<![a-z0-9]){escaped_title}(?![a-z0-9])",
            normalized_question,
        ) is not None

    compact_question = re.sub(r"[\W_]+", "", normalized_question)
    compact_title = re.sub(r"[\W_]+", "", normalized_title)
    return len(compact_title) >= 2 and compact_title in compact_question


def find_high_confidence_title_pages(
    question: str,
    index_content: str,
    wiki_dir: Path,
) -> list[Path]:
    """返回问题中完整提及的安全知识页，不包含 overview 或图谱扩展页。"""
    pages: list[Path] = []
    for title, href in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", index_content):
        if not is_high_confidence_title_match(question, title):
            continue
        page = resolve_wiki_page(wiki_dir, href)
        if page is None or not is_knowledge_page(wiki_dir=wiki_dir, path=page):
            continue
        if page.name == "overview.md" or page in pages:
            continue
        pages.append(page)
    return pages


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
            page = resolve_wiki_page(wiki_dir, href)
            if page is not None and is_knowledge_page(wiki_dir=wiki_dir, path=page) and page not in relevant:
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
                neighbor = resolve_wiki_page(wiki_dir, f"{node_id}.md")
                if neighbor is not None and is_knowledge_page(wiki_dir=wiki_dir, path=neighbor) and neighbor not in relevant:
                    relevant.append(neighbor)
        except (json.JSONDecodeError, KeyError, TypeError):
            LOGGER.warning("Failed to expand relevant pages from graph.json", exc_info=True)

    overview = wiki_dir / "overview.md"
    if is_knowledge_page(wiki_dir=wiki_dir, path=overview) and overview not in relevant:
        relevant.insert(0, overview)
    return relevant[:15]


class QueryService:
    def __init__(self, agent_root: Path) -> None:
        self._agent_root = agent_root.resolve()
        self._wiki_dir = self._agent_root / "wiki"
        self._index_file = self._wiki_dir / "index.md"
        self._graph_json = self._agent_root / "graph" / "graph.json"
        self._call_llm_fast: Callable[[str, int | None], str] | None = None
        self._call_llm_main: Callable[[str, int | None], str] | None = None

    def run(self, question: str) -> QueryResult:
        return self._run(question=question, history_messages=[], use_wiki_links=False)

    def run_chat_turn(
        self,
        question: str,
        history_messages: Sequence[ChatMessageResponse],
        model_profile: LLMProfile | None = None,
    ) -> QueryResult:
        kwargs = {
            "question": question,
            "history_messages": list(history_messages)[-6:],
            "use_wiki_links": True,
        }
        if model_profile is not None:
            kwargs["answer_model_profile"] = model_profile
        return self._run(
            **kwargs,
        )

    def _run(
        self,
        question: str,
        history_messages: Sequence[ChatMessageResponse],
        use_wiki_links: bool,
        answer_model_profile: LLMProfile | None = None,
    ) -> QueryResult:
        normalized_question = question.strip()
        if not normalized_question:
            raise QueryServiceError("question cannot be empty")

        index_content = read_file(self._index_file)
        if not index_content:
            raise QueryServiceError("Wiki is empty. Ingest some sources first before querying.")

        relevant_pages = self._select_relevant_pages(
            normalized_question,
            index_content,
            history_messages=history_messages,
        )
        pages_context = self._build_pages_context(relevant_pages, index_content)
        sources = self._build_stable_sources(relevant_pages)
        citations = self._build_citations(sources=sources, relevant_pages=relevant_pages)
        prompt = self._build_answer_prompt(
            question=normalized_question,
            schema=load_prompt("agent_instructions.md"),
            pages_context=pages_context,
            conversation_history=self._build_conversation_history(history_messages),
            sources=sources,
            citations=citations,
            use_wiki_links=use_wiki_links,
        )
        _, call_llm_main = self._get_llm_callers()

        answer_model = answer_model_profile or LLMProfile(
            provider=settings.llm_provider,
            model=settings.llm_main_model,
            api_key=None,
            api_base=None,
            max_tokens=settings.llm_main_max_tokens,
            temperature=settings.llm_main_temperature,
        )
        model_role = "chat_profile" if answer_model_profile is not None else "internal_main"
        answer_started_at = time.monotonic()
        LOGGER.info(
            "Wiki answer generation started model_role=%s provider=%s model=%s "
            "reasoning_effort=%s relevant_pages=%s history_messages=%s",
            model_role,
            answer_model.provider,
            answer_model.model,
            answer_model.reasoning_effort or "provider_default",
            len(relevant_pages),
            len(history_messages),
        )

        try:
            if answer_model_profile is None:
                answer = self._call_llm_with_retry(
                    call_llm_main,
                    prompt,
                    max_tokens=settings.llm_main_max_tokens,
                    operation="answer generation",
                )
            else:
                answer = self._call_llm_with_retry(
                    lambda request_prompt, max_tokens: call_llm_profile(
                        request_prompt,
                        answer_model_profile,
                        max_tokens=max_tokens,
                    ),
                    prompt,
                    max_tokens=answer_model_profile.max_tokens,
                    operation="answer generation",
                )
        except Exception as exc:
            LOGGER.exception(
                "Wiki answer generation failed model_role=%s provider=%s model=%s elapsed_ms=%s",
                model_role,
                answer_model.provider,
                answer_model.model,
                round((time.monotonic() - answer_started_at) * 1000),
            )
            raise QueryServiceError("Failed to generate query answer via backend LLM config.") from exc

        normalized_answer = self._normalize_answer(answer, source_count=len(sources))
        if use_wiki_links:
            normalized_answer = self._replace_inline_citations_with_wiki_links(
                normalized_answer,
                citations,
            )
        if not normalized_answer:
            raise QueryServiceError("LLM returned an empty answer")

        LOGGER.info(
            "Wiki answer generation completed model_role=%s provider=%s model=%s "
            "reasoning_effort=%s elapsed_ms=%s answer_chars=%s relevant_pages=%s citations=%s",
            model_role,
            answer_model.provider,
            answer_model.model,
            answer_model.reasoning_effort or "provider_default",
            round((time.monotonic() - answer_started_at) * 1000),
            len(normalized_answer),
            len(relevant_pages),
            len(citations),
        )

        return QueryResult(
            answer=normalized_answer,
            sources=sources,
            relevant_pages=[page.relative_to(self._wiki_dir).as_posix() for page in relevant_pages],
            citations=citations,
        )

    def _select_relevant_pages(
        self,
        question: str,
        index_content: str,
        *,
        history_messages: Sequence[ChatMessageResponse] = (),
    ) -> list[Path]:
        relevant_pages = find_relevant_pages(question, index_content, self._wiki_dir, self._graph_json)
        high_confidence_pages = find_high_confidence_title_pages(
            question,
            index_content,
            self._wiki_dir,
        )
        if 0 < len(high_confidence_pages) <= MAX_DIRECT_HIGH_CONFIDENCE_PAGES:
            overview = self._wiki_dir / "overview.md"
            selected_pages = (
                [overview, *high_confidence_pages]
                if is_knowledge_page(wiki_dir=self._wiki_dir, path=overview)
                else high_confidence_pages
            )
            LOGGER.info(
                "Wiki page selection completed strategy=high_confidence_title "
                "selected_pages=%s",
                len(selected_pages),
            )
            return selected_pages

        selection_started_at = time.monotonic()
        LOGGER.info(
            "Wiki page selection started strategy=model_fallback model_role=internal_fast_selector "
            "provider=%s model=%s reasoning_effort=provider_default",
            settings.llm_provider,
            settings.llm_fast_model,
        )
        call_llm_fast, _ = self._get_llm_callers()
        history_context = self._build_page_selection_history(history_messages)
        history_section = (
            f"\n\nRecent conversation context (use only to resolve references):\n{history_context}"
            if history_context != "(none)"
            else ""
        )
        prompt = (
            "Given this wiki index:\n\n"
            f"{index_content}\n\n"
            f"{history_section}\n\n"
            f'Which pages are most relevant to answering: "{question}"\n\n'
            'Return ONLY a JSON array of relative file paths (as listed in the index), '
            'e.g. ["sources/foo.md", "concepts/Bar.md"]. Maximum 10 pages.'
        )
        try:
            raw = self._call_llm_with_retry(
                call_llm_fast,
                prompt,
                max_tokens=settings.llm_fast_max_tokens,
                operation="page selection",
            )
        except Exception:
            LOGGER.warning(
                "Wiki page selection failed strategy=model_fallback model_role=internal_fast_selector "
                "elapsed_ms=%s",
                round((time.monotonic() - selection_started_at) * 1000),
                exc_info=True,
            )
            return relevant_pages

        parsed_paths = self._parse_json_array(raw)
        if parsed_paths is None:
            LOGGER.warning(
                "Wiki page selection returned invalid response strategy=model_fallback "
                "model_role=internal_fast_selector elapsed_ms=%s",
                round((time.monotonic() - selection_started_at) * 1000),
            )
            return relevant_pages

        selected_pages: list[Path] = []
        for path_text in parsed_paths:
            if not isinstance(path_text, str):
                continue
            candidate = self._resolve_wiki_page(path_text)
            if candidate is not None and candidate not in selected_pages:
                selected_pages.append(candidate)
                if len(selected_pages) == MAX_MODEL_SELECTED_PAGES:
                    break

        final_pages = selected_pages or relevant_pages
        LOGGER.info(
            "Wiki page selection completed strategy=model_fallback model_role=internal_fast_selector "
            "elapsed_ms=%s selected_pages=%s",
            round((time.monotonic() - selection_started_at) * 1000),
            len(final_pages),
        )
        return final_pages

    def _resolve_wiki_page(self, value: str) -> Path | None:
        candidate = resolve_wiki_page(self._wiki_dir, value)
        return candidate if candidate is not None and self._is_safe_wiki_page(candidate) else None

    def _build_citations(
        self,
        *,
        sources: list[str],
        relevant_pages: list[Path],
    ) -> list[CitationResponse]:
        safe_relevant_pages = [
            page.resolve()
            for page in relevant_pages
            if self._is_safe_wiki_page(page)
        ]
        ordered_pages: list[Path] = []
        for source in sources:
            page = self._resolve_source_reference(source, safe_relevant_pages)
            if page is not None and page not in ordered_pages:
                ordered_pages.append(page)
        return [self._citation_from_page(page) for page in ordered_pages]

    def _build_stable_sources(self, relevant_pages: Sequence[Path]) -> list[str]:
        """Return safe Wiki-relative evidence paths in retrieval order."""
        sources: list[str] = []
        for page in relevant_pages:
            if not self._is_safe_wiki_page(page):
                continue
            source = page.resolve().relative_to(self._wiki_dir.resolve()).as_posix()
            if source not in sources:
                sources.append(source)
        return sources

    @staticmethod
    def _normalize_answer(answer: str, *, source_count: int) -> str:
        """Remove legacy source lists and invalid inline citation markers."""
        without_sources = TRAILING_SOURCES_SECTION_PATTERN.sub("", answer).strip()

        def replace_marker(match: re.Match[str]) -> str:
            marker = int(match.group(1))
            return f"[{marker}]" if 1 <= marker <= source_count else ""

        return INLINE_CITATION_PATTERN.sub(replace_marker, without_sources).strip()

    @staticmethod
    def _replace_inline_citations_with_wiki_links(
        answer: str,
        citations: Sequence[CitationResponse],
    ) -> str:
        """Convert valid numeric chat citations to their rendered Wiki links."""

        def replace_marker(match: re.Match[str]) -> str:
            citation_index = int(match.group(1)) - 1
            if not 0 <= citation_index < len(citations):
                return ""
            citation = citations[citation_index]
            return QueryService._format_wiki_link(citation)

        return INLINE_CITATION_PATTERN.sub(replace_marker, answer)

    def _resolve_source_reference(self, source: str, preferred_pages: list[Path]) -> Path | None:
        reference = source.split("|", 1)[0].split("#", 1)[0].strip().replace("\\", "/")
        if not reference:
            return None
        relative_reference = reference[:-3] if reference.lower().endswith(".md") else reference
        folded_reference = relative_reference.casefold()
        preferred_matches = [
            page
            for page in preferred_pages
            if self._page_matches_reference(page, folded_reference)
        ]
        if len(preferred_matches) == 1:
            return preferred_matches[0]
        if "/" in reference:
            return self._resolve_wiki_page(reference)
        matches = [
            page.resolve()
            for page in self._wiki_dir.rglob("*.md")
            if page.stem.casefold() == folded_reference and self._is_safe_wiki_page(page)
        ]
        return matches[0] if len(matches) == 1 else None

    def _page_matches_reference(self, page: Path, folded_reference: str) -> bool:
        relative = page.relative_to(self._wiki_dir).as_posix()
        without_suffix = relative[:-3] if relative.lower().endswith(".md") else relative
        return without_suffix.casefold() == folded_reference or page.stem.casefold() == folded_reference

    def _is_safe_wiki_page(self, page: Path) -> bool:
        try:
            resolved = page.resolve()
            resolved.relative_to(self._wiki_dir.resolve())
        except (OSError, ValueError):
            return False
        return is_knowledge_page(wiki_dir=self._wiki_dir, path=resolved)

    def _citation_from_page(self, page: Path) -> CitationResponse:
        relative = page.relative_to(self._wiki_dir).as_posix()
        content = page.read_text(encoding="utf-8")
        metadata = self._read_frontmatter_fields(content)
        title = metadata.get("title") or self._read_first_heading(content) or page.stem
        metadata_kind = metadata.get("type", "").lower()
        directory_kind = DIRECTORY_KINDS.get(Path(relative).parts[0].lower())
        kind_text = metadata_kind if metadata_kind in KNOWN_CITATION_KINDS else directory_kind or "page"
        return CitationResponse(
            path=relative,
            title=title,
            kind=cast(CitationKind, kind_text),
        )

    @staticmethod
    def _read_frontmatter_fields(content: str) -> dict[str, str]:
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.startswith("---\n"):
            return {}
        closing = normalized.find("\n---", 4)
        if closing == -1:
            return {}
        fields: dict[str, str] = {}
        for key, raw_value in FRONTMATTER_FIELD_PATTERN.findall(normalized[4:closing]):
            value = raw_value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if value:
                fields[key] = value
        return fields

    @staticmethod
    def _read_first_heading(content: str) -> str | None:
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        match = HEADING_PATTERN.search(normalized)
        return match.group(1).strip() if match else None

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
    def _build_page_selection_history(history_messages: Sequence[ChatMessageResponse]) -> str:
        """构造有长度上限的追问上下文，避免检索阶段重复携带完整聊天记录。"""
        if not history_messages:
            return "(none)"

        remaining_chars = PAGE_SELECTION_HISTORY_MAX_CHARS
        lines: list[str] = []
        for message in history_messages[-PAGE_SELECTION_HISTORY_MESSAGE_LIMIT:]:
            role_label = "User" if message.role == "user" else "Assistant"
            normalized_content = " ".join(message.content.split())
            prefix = f"{role_label}: "
            available_chars = min(
                PAGE_SELECTION_HISTORY_MESSAGE_MAX_CHARS,
                remaining_chars - len(prefix),
            )
            if available_chars <= 0:
                break
            content = normalized_content[:available_chars]
            if len(normalized_content) > available_chars:
                content = content.rstrip() + "…"
            line = f"{prefix}{content}"
            lines.append(line)
            remaining_chars -= len(line) + 1
            if remaining_chars <= 0:
                break
        return "\n".join(lines) or "(none)"

    @staticmethod
    def _build_answer_prompt(
        question: str,
        schema: str,
        pages_context: str,
        conversation_history: str,
        sources: Sequence[str],
        citations: Sequence[CitationResponse],
        use_wiki_links: bool,
    ) -> str:
        if use_wiki_links:
            citation_instructions = (
                "- Use only the Wiki links below for citations. For each key conclusion with clear "
                "support, add the matching Wiki link at the end of the sentence.\n"
                "- Do not use numbered `[n]` citations or any other citation syntax."
            )
            evidence_sources = "\n".join(
                QueryService._format_wiki_link(citation) for citation in citations
            ) or "(none)"
        else:
            citation_instructions = (
                "- Use only the numbered evidence sources below for citations. For each key conclusion "
                "with clear support, add its source marker at the end of the sentence as `[n]`.\n"
                "- Do not use `[[PageName]]` wikilinks or any other citation syntax."
            )
            evidence_sources = "\n".join(
                f"[{index}] {source}" for index, source in enumerate(sources, start=1)
            ) or "(none)"
        return render_prompt(
            "query.md",
            question=question,
            schema=schema,
            pages_context=pages_context,
            conversation_history=conversation_history,
            citation_instructions=citation_instructions,
            sources=evidence_sources,
        )

    @staticmethod
    def _format_wiki_link(citation: CitationResponse) -> str:
        target = citation.path.removesuffix(".md")
        return f"[[{target}|{citation.title}]]"

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
        return call_llm_fast, call_llm_main
