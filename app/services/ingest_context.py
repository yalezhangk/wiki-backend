"""Ingest 的本地 token 预算和确定性 Wiki 上下文检索。"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path


SMALL_SOURCE_MAX_TOKENS = 24_576
WIKI_CONTEXT_MAX_TOKENS = 16_384
SECTION_MAX_TOKENS = 4_096
_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}")
_WIKILINK_PATTERN = re.compile(r"\[\[([^\]|#]+)")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_STOP_WORDS = frozenset({"about", "after", "and", "are", "for", "from", "into", "that", "the", "this", "with", "以及", "一个", "一些", "我们", "本文", "相关", "通过"})


class TokenEstimator:
    """无厂商 tokenizer 时的保守估算，绝不表示精确 token 数。"""

    @staticmethod
    def estimate(text: str) -> int:
        ascii_word_characters = sum(character.isascii() and character.isalnum() for character in text)
        return len(text) - ascii_word_characters + math.ceil(ascii_word_characters / 4)


@dataclass(frozen=True)
class PromptBudget:
    max_input_tokens: int
    max_output_tokens: int
    safety_margin_tokens: int
    context_window_tokens: int

    def accepts_input(self, input_tokens: int) -> bool:
        return (
            input_tokens <= self.max_input_tokens
            and input_tokens + self.max_output_tokens + self.safety_margin_tokens
            <= self.context_window_tokens
        )


@dataclass(frozen=True)
class WikiPage:
    path: str
    content: str
    sha256: str


@dataclass(frozen=True)
class WikiSnapshot:
    pages: tuple[WikiPage, ...]

    @classmethod
    def capture(cls, wiki_dir: Path) -> "WikiSnapshot":
        paths = [wiki_dir / "index.md", wiki_dir / "overview.md"]
        for directory in ("sources", "entities", "concepts"):
            paths.extend((wiki_dir / directory).rglob("*.md") if (wiki_dir / directory).is_dir() else [])
        pages: list[WikiPage] = []
        for path in sorted(paths, key=lambda item: item.as_posix()):
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            pages.append(
                WikiPage(
                    path=path.relative_to(wiki_dir.parent).as_posix(),
                    content=content,
                    sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )
        return cls(pages=tuple(pages))


@dataclass(frozen=True)
class WikiCandidate:
    path: str
    heading: str
    page_type: str
    content: str
    snapshot_hash: str
    score: int

    @property
    def rendered(self) -> str:
        return (
            f"[path: {self.path}; heading: {self.heading}; snapshot: {self.snapshot_hash}]\n"
            f"{self.content.strip()}"
        )


class WikiContextRetriever:
    """使用来源词法特征从固定快照中选择受限 Wiki 证据。"""

    def __init__(self, snapshot: WikiSnapshot) -> None:
        self._snapshot = snapshot

    def candidates(self, source_content: str) -> list[WikiCandidate]:
        query_terms, links = self._query_terms(source_content)
        candidates: list[WikiCandidate] = []
        for page in self._snapshot.pages:
            if page.path == "wiki/index.md":
                continue
            page_type = page.path.split("/", 2)[1] if page.path.count("/") >= 1 else "overview"
            for heading, section in self._sections(page.content):
                score = self._score(heading, section, query_terms, links)
                if score:
                    candidates.append(
                        WikiCandidate(page.path, heading, page_type, section, page.sha256, score)
                    )
        return sorted(candidates, key=lambda item: (-item.score, item.path, item.heading))

    def select(self, source_content: str, available_tokens: int) -> tuple[list[WikiCandidate], list[str]]:
        if available_tokens <= 0:
            return [], ["budget_zero"]
        selected: list[WikiCandidate] = []
        skipped: list[str] = []
        used = 0
        candidates = self.candidates(source_content)
        overview = [item for item in candidates if item.page_type == "overview"]
        others = [item for item in candidates if item.page_type != "overview"]
        # 先保证 Overview 和其他类型各有一次机会，再按原始稳定顺序补齐。
        first_by_type: list[WikiCandidate] = []
        known_types: set[str] = set()
        for candidate in [*overview, *others]:
            if candidate.page_type not in known_types:
                first_by_type.append(candidate)
                known_types.add(candidate.page_type)
        ordered = [*first_by_type, *[item for item in [*overview, *others] if item not in first_by_type]]
        for candidate in ordered:
            tokens = TokenEstimator.estimate(candidate.rendered)
            if tokens > SECTION_MAX_TOKENS:
                skipped.append(f"section_too_large:{candidate.path}:{candidate.heading}")
                continue
            if used + tokens > available_tokens:
                skipped.append(f"budget_exhausted:{candidate.path}:{candidate.heading}")
                continue
            selected.append(candidate)
            used += tokens
        if not selected and not skipped:
            skipped.append("no_matching_candidate")
        return selected, skipped

    @staticmethod
    def render(selected: list[WikiCandidate]) -> str:
        if not selected:
            return "(no relevant existing Wiki evidence was retrieved)"
        return "\n\n---\n\n".join(item.rendered for item in selected)

    @staticmethod
    def _query_terms(source_content: str) -> tuple[set[str], set[str]]:
        terms = {word.casefold() for word in _WORD_PATTERN.findall(source_content)}
        links = {link.strip().casefold() for link in _WIKILINK_PATTERN.findall(source_content)}
        terms.difference_update(_STOP_WORDS)
        return terms, links

    @staticmethod
    def _sections(content: str) -> list[tuple[str, str]]:
        matches = list(_HEADING_PATTERN.finditer(content))
        if not matches:
            return [("(document)", content)]
        sections: list[tuple[str, str]] = []
        prefix = content[: matches[0].start()].strip()
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            section = (prefix + "\n" if prefix else "") + content[match.start() : end]
            sections.append((match.group(2).strip(), section.strip()))
            prefix = ""
        return sections

    @staticmethod
    def _score(heading: str, section: str, terms: set[str], links: set[str]) -> int:
        heading_terms = {word.casefold() for word in _WORD_PATTERN.findall(heading)}
        section_terms = {word.casefold() for word in _WORD_PATTERN.findall(section)}
        score = 4 * len(heading_terms & terms) + len(section_terms & terms)
        normalized_heading = heading.casefold()
        score += 10 * sum(link == normalized_heading for link in links)
        return score
