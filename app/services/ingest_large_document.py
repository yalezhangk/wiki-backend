"""大文档 Ingest 的确定性 Markdown 分块。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Literal

from app.services.ingest_context import TokenEstimator
from app.services.ingest_large_document_status import (
    LargeDocumentProgressCallback,
    LargeDocumentProgressEvent,
)


LARGE_SOURCE_CHUNK_TARGET_TOKENS = 10_000
LARGE_SOURCE_CHUNK_MAX_TOKENS = 16_000
LARGE_SOURCE_CHUNK_OVERLAP_TOKENS = 384

MarkdownBlockKind = Literal[
    "heading",
    "paragraph",
    "list",
    "table",
    "fenced_code",
    "blank",
]

_ATX_HEADING_PATTERN = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_START_PATTERN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_LIST_ITEM_PATTERN = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+")
_TOC_ENTRY_PATTERN = re.compile(
    r"^(?P<title>.{2,80}?)(?:[ \t]*(?:\.{2,}|…{2,})[ \t]*|[ \t]{2,})"
    r"(?P<page>\d{1,4})$"
)
_NUMBERED_HEADING_PATTERN = re.compile(r"^(?P<number>\d+(?:\.\d+)*)[.)、]?[ \t]+")


class MarkdownChunkingError(ValueError):
    """Markdown 无法在结构约束和 token 硬上限内安全分块。"""


@dataclass(frozen=True)
class MarkdownBlock:
    """连续且不重排的 Markdown 来源区间。"""

    kind: MarkdownBlockKind
    start_line: int
    end_line: int
    heading_path: tuple[str, ...]
    text: str
    context_text: str = ""
    must_stand_alone: bool = False

    @property
    def estimated_tokens(self) -> int:
        return TokenEstimator.estimate(self.text)


@dataclass(frozen=True)
class MarkdownChunk:
    """供后续 evidence extraction 使用的稳定内部来源单元。"""

    chunk_id: str
    start_line: int
    end_line: int
    breadcrumbs: tuple[str, ...]
    primary_text: str
    overlap_text: str
    structural_context: str
    estimated_tokens: int


class MarkdownChunker:
    """按 Markdown 结构和保守 token 估算确定性地切分来源。"""

    def __init__(
        self,
        *,
        target_tokens: int = LARGE_SOURCE_CHUNK_TARGET_TOKENS,
        max_tokens: int = LARGE_SOURCE_CHUNK_MAX_TOKENS,
        overlap_tokens: int = LARGE_SOURCE_CHUNK_OVERLAP_TOKENS,
    ) -> None:
        if target_tokens <= 0:
            raise ValueError("target_tokens must be positive")
        if max_tokens < target_tokens:
            raise ValueError("max_tokens must be greater than or equal to target_tokens")
        if overlap_tokens < 0 or overlap_tokens >= max_tokens:
            raise ValueError("overlap_tokens must be non-negative and less than max_tokens")
        self._target_tokens = target_tokens
        self._max_tokens = max_tokens
        self._overlap_tokens = overlap_tokens

    def parse_blocks(self, source_content: str) -> tuple[MarkdownBlock, ...]:
        """解析顺序 block，并保留原始文本、行号和章节路径。"""
        if not source_content:
            raise MarkdownChunkingError("Markdown source cannot be empty")

        lines = source_content.splitlines(keepends=True)
        synthetic_headings = self._detect_directory_headings(lines)
        heading_stack: dict[int, str] = {}
        blocks: list[MarkdownBlock] = []
        index = 0

        while index < len(lines):
            start = index
            line = lines[index]
            heading = self._heading_at(index, line, synthetic_headings)
            fence = _FENCE_START_PATTERN.match(line.rstrip("\r\n"))

            if heading is not None:
                level, title = heading
                heading_stack = {
                    known_level: known_title
                    for known_level, known_title in heading_stack.items()
                    if known_level < level
                }
                heading_stack[level] = title
                index += 1
                kind: MarkdownBlockKind = "heading"
            elif fence is not None:
                index = self._consume_fenced_code(lines, index, fence.group(1))
                kind = "fenced_code"
            elif self._is_blank(line):
                while index < len(lines) and self._is_blank(lines[index]):
                    index += 1
                kind = "blank"
            elif self._starts_table(lines, index):
                index += 2
                while index < len(lines) and self._is_table_row(lines[index]):
                    index += 1
                kind = "table"
            elif _LIST_ITEM_PATTERN.match(line.rstrip("\r\n")):
                index += 1
                while index < len(lines):
                    candidate = lines[index]
                    if self._is_blank(candidate):
                        break
                    if (
                        self._heading_at(index, candidate, synthetic_headings) is not None
                        or _FENCE_START_PATTERN.match(candidate.rstrip("\r\n")) is not None
                        or self._starts_table(lines, index)
                    ):
                        break
                    index += 1
                kind = "list"
            else:
                index += 1
                while index < len(lines):
                    if self._is_blank(lines[index]) or self._is_special_start(
                        lines,
                        index,
                        synthetic_headings,
                    ):
                        break
                    index += 1
                kind = "paragraph"

            blocks.append(
                MarkdownBlock(
                    kind=kind,
                    start_line=start + 1,
                    end_line=index,
                    heading_path=tuple(
                        heading_stack[level] for level in sorted(heading_stack)
                    ),
                    text="".join(lines[start:index]),
                )
            )

        self._validate_block_coverage(source_content, blocks)
        return tuple(blocks)

    def chunk(
        self,
        source_content: str,
        *,
        progress_callback: LargeDocumentProgressCallback | None = None,
    ) -> tuple[MarkdownChunk, ...]:
        """生成具有稳定 ID、显式上下文和完整覆盖保证的 chunks。"""
        if progress_callback is not None:
            progress_callback(
                LargeDocumentProgressEvent(
                    phase="chunking",
                    completed=0,
                    total=1,
                )
            )
        parsed_blocks = self.parse_blocks(source_content)
        blocks = tuple(
            split_block
            for block in parsed_blocks
            for split_block in self._fit_block_to_hard_limit(block)
        )
        grouped_blocks = self._group_blocks(blocks)
        chunks = self._build_chunks(grouped_blocks)
        self._validate_chunk_coverage(source_content, chunks)
        if progress_callback is not None:
            progress_callback(
                LargeDocumentProgressEvent(
                    phase="chunking",
                    completed=1,
                    total=1,
                    item_id=f"chunks:{len(chunks)}",
                )
            )
        return chunks

    def _group_blocks(
        self,
        blocks: tuple[MarkdownBlock, ...],
    ) -> tuple[tuple[MarkdownBlock, ...], ...]:
        sections: list[list[MarkdownBlock]] = []
        section: list[MarkdownBlock] = []
        for block in blocks:
            if block.kind == "heading" and section:
                sections.append(section)
                section = []
            section.append(block)
        if section:
            sections.append(section)

        groups: list[tuple[MarkdownBlock, ...]] = []
        current: list[MarkdownBlock] = []
        for candidate_section in sections:
            if any(block.must_stand_alone for block in candidate_section):
                if current:
                    groups.append(tuple(current))
                    current = []
                self._append_blocks_with_limits(groups, candidate_section)
                continue

            candidate_tokens = self._estimate_blocks([*current, *candidate_section])
            section_tokens = self._estimate_blocks(candidate_section)
            if current and candidate_tokens > self._target_tokens:
                groups.append(tuple(current))
                current = []
            if section_tokens > self._max_tokens:
                self._append_blocks_with_limits(groups, candidate_section)
            else:
                current.extend(candidate_section)

        if current:
            groups.append(tuple(current))
        return tuple(groups)

    def _append_blocks_with_limits(
        self,
        groups: list[tuple[MarkdownBlock, ...]],
        blocks: list[MarkdownBlock],
    ) -> None:
        current: list[MarkdownBlock] = []
        for block in blocks:
            if block.must_stand_alone:
                if current:
                    groups.append(tuple(current))
                    current = []
                groups.append((block,))
                continue

            candidate_tokens = self._estimate_blocks([*current, block])
            if current and (
                self._estimate_blocks(current) >= self._target_tokens
                or candidate_tokens > self._max_tokens
            ):
                groups.append(tuple(current))
                current = []
            current.append(block)
        if current:
            groups.append(tuple(current))

    def _build_chunks(
        self,
        grouped_blocks: tuple[tuple[MarkdownBlock, ...], ...],
    ) -> tuple[MarkdownChunk, ...]:
        chunks: list[MarkdownChunk] = []
        previous_blocks: tuple[MarkdownBlock, ...] = ()
        for index, blocks in enumerate(grouped_blocks, start=1):
            primary_text = "".join(block.text for block in blocks)
            structural_context = "".join(
                dict.fromkeys(block.context_text for block in blocks if block.context_text)
            )
            base_text = structural_context + primary_text
            base_tokens = TokenEstimator.estimate(base_text)
            if base_tokens > self._max_tokens:
                raise MarkdownChunkingError(
                    f"chunk {index} exceeds hard token limit after structural context"
                )

            overlap_candidate = self._trailing_plain_text(previous_blocks)
            overlap_text = self._bounded_overlap(
                overlap_candidate,
                base_text=base_text,
            )
            full_text = overlap_text + base_text
            breadcrumbs = tuple(
                dict.fromkeys(
                    " / ".join(block.heading_path)
                    for block in blocks
                    if block.heading_path
                )
            )
            digest = hashlib.sha256(primary_text.encode("utf-8")).hexdigest()[:12]
            chunks.append(
                MarkdownChunk(
                    chunk_id=f"chunk-{index:04d}-{digest}",
                    start_line=blocks[0].start_line,
                    end_line=blocks[-1].end_line,
                    breadcrumbs=breadcrumbs,
                    primary_text=primary_text,
                    overlap_text=overlap_text,
                    structural_context=structural_context,
                    estimated_tokens=TokenEstimator.estimate(full_text),
                )
            )
            previous_blocks = blocks
        return tuple(chunks)

    def _fit_block_to_hard_limit(
        self,
        block: MarkdownBlock,
    ) -> tuple[MarkdownBlock, ...]:
        if block.estimated_tokens <= self._max_tokens:
            return (block,)
        if block.kind in {"paragraph", "blank"}:
            return self._split_plain_block(block)
        if block.kind == "table":
            return self._split_table_block(block)
        raise MarkdownChunkingError(
            f"{block.kind} block at lines {block.start_line}-{block.end_line} "
            f"exceeds hard token limit {self._max_tokens}"
        )

    def _split_plain_block(self, block: MarkdownBlock) -> tuple[MarkdownBlock, ...]:
        pieces: list[MarkdownBlock] = []
        offset = 0
        while offset < len(block.text):
            length = self._largest_prefix_within_tokens(
                block.text[offset:],
                self._max_tokens,
            )
            if length <= 0:
                raise MarkdownChunkingError(
                    f"paragraph at line {block.start_line} cannot be split safely"
                )
            preferred = self._preferred_text_boundary(block.text[offset:], length)
            piece_text = block.text[offset : offset + preferred]
            start_line, end_line = self._piece_line_range(block, offset, piece_text)
            pieces.append(
                replace(
                    block,
                    start_line=start_line,
                    end_line=end_line,
                    text=piece_text,
                )
            )
            offset += preferred
        return tuple(pieces)

    def _split_table_block(self, block: MarkdownBlock) -> tuple[MarkdownBlock, ...]:
        lines = block.text.splitlines(keepends=True)
        header = "".join(lines[:2])
        if TokenEstimator.estimate(header) > self._max_tokens:
            raise MarkdownChunkingError(
                f"table header at lines {block.start_line}-{block.start_line + 1} "
                f"exceeds hard token limit {self._max_tokens}"
            )

        fragments: list[MarkdownBlock] = []
        current_lines = list(lines[:2])
        current_start_offset = 0
        consumed_lines = 2
        for row in lines[2:]:
            context = "" if not fragments else header
            candidate = "".join([*current_lines, row])
            if TokenEstimator.estimate(context + candidate) <= self._max_tokens:
                current_lines.append(row)
                consumed_lines += 1
                continue

            if len(current_lines) == (2 if not fragments else 0):
                row_line = block.start_line + consumed_lines
                raise MarkdownChunkingError(
                    f"table row at line {row_line} exceeds hard token limit "
                    f"{self._max_tokens}"
                )
            fragments.append(
                self._table_fragment(
                    block,
                    text="".join(current_lines),
                    context_text="" if not fragments else header,
                    line_offset=current_start_offset,
                )
            )
            current_start_offset = consumed_lines
            current_lines = [row]
            consumed_lines += 1
            if TokenEstimator.estimate(header + row) > self._max_tokens:
                row_line = block.start_line + current_start_offset
                raise MarkdownChunkingError(
                    f"table row at line {row_line} exceeds hard token limit "
                    f"{self._max_tokens}"
                )

        if current_lines:
            fragments.append(
                self._table_fragment(
                    block,
                    text="".join(current_lines),
                    context_text="" if not fragments else header,
                    line_offset=current_start_offset,
                )
            )
        return tuple(fragments)

    @staticmethod
    def _table_fragment(
        block: MarkdownBlock,
        *,
        text: str,
        context_text: str,
        line_offset: int,
    ) -> MarkdownBlock:
        line_count = max(1, len(text.splitlines()))
        return replace(
            block,
            start_line=block.start_line + line_offset,
            end_line=block.start_line + line_offset + line_count - 1,
            text=text,
            context_text=context_text,
            must_stand_alone=True,
        )

    def _bounded_overlap(self, candidate: str, *, base_text: str) -> str:
        if not candidate or self._overlap_tokens == 0:
            return ""
        overlap = self._largest_suffix_within_tokens(candidate, self._overlap_tokens)
        while overlap and TokenEstimator.estimate(overlap + base_text) > self._max_tokens:
            overlap = overlap[1:]
        return overlap

    @staticmethod
    def _trailing_plain_text(blocks: tuple[MarkdownBlock, ...]) -> str:
        trailing: list[str] = []
        for block in reversed(blocks):
            if block.kind == "paragraph":
                trailing.append(block.text)
                continue
            if block.kind == "blank" and trailing:
                trailing.append(block.text)
                continue
            break
        return "".join(reversed(trailing))

    @staticmethod
    def _largest_prefix_within_tokens(text: str, max_tokens: int) -> int:
        low = 1
        high = len(text)
        accepted = 0
        while low <= high:
            middle = (low + high) // 2
            if TokenEstimator.estimate(text[:middle]) <= max_tokens:
                accepted = middle
                low = middle + 1
            else:
                high = middle - 1
        return accepted

    @staticmethod
    def _largest_suffix_within_tokens(text: str, max_tokens: int) -> str:
        low = 0
        high = len(text)
        accepted_start = len(text)
        while low <= high:
            middle = (low + high) // 2
            if TokenEstimator.estimate(text[middle:]) <= max_tokens:
                accepted_start = middle
                high = middle - 1
            else:
                low = middle + 1
        return text[accepted_start:]

    @staticmethod
    def _preferred_text_boundary(text: str, maximum: int) -> int:
        if maximum >= len(text):
            return maximum
        minimum = max(1, maximum // 2)
        for character in ("\n", "。", "！", "？", ". ", "; ", " "):
            boundary = text.rfind(character, minimum, maximum)
            if boundary >= minimum:
                return boundary + len(character)
        return maximum

    @staticmethod
    def _piece_line_range(
        block: MarkdownBlock,
        offset: int,
        piece_text: str,
    ) -> tuple[int, int]:
        start_line = block.start_line + block.text[:offset].count("\n")
        end_line = start_line + piece_text.count("\n")
        if piece_text.endswith(("\n", "\r")):
            end_line -= 1
        return start_line, max(start_line, end_line)

    @staticmethod
    def _estimate_blocks(blocks: list[MarkdownBlock]) -> int:
        return TokenEstimator.estimate("".join(block.text for block in blocks))

    @classmethod
    def _is_special_start(
        cls,
        lines: list[str],
        index: int,
        synthetic_headings: dict[int, tuple[int, str]],
    ) -> bool:
        line = lines[index]
        return (
            cls._heading_at(index, line, synthetic_headings) is not None
            or _FENCE_START_PATTERN.match(line.rstrip("\r\n")) is not None
            or cls._starts_table(lines, index)
            or _LIST_ITEM_PATTERN.match(line.rstrip("\r\n")) is not None
        )

    @staticmethod
    def _heading_at(
        index: int,
        line: str,
        synthetic_headings: dict[int, tuple[int, str]],
    ) -> tuple[int, str] | None:
        explicit = _ATX_HEADING_PATTERN.match(line.rstrip("\r\n"))
        if explicit is not None:
            return len(explicit.group(1)), explicit.group(2).strip()
        return synthetic_headings.get(index)

    @staticmethod
    def _consume_fenced_code(lines: list[str], start: int, marker: str) -> int:
        index = start + 1
        closing = re.compile(
            rf"^[ \t]{{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$"
        )
        while index < len(lines):
            if closing.match(lines[index].rstrip("\r\n")):
                return index + 1
            index += 1
        return len(lines)

    @classmethod
    def _starts_table(cls, lines: list[str], index: int) -> bool:
        return (
            index + 1 < len(lines)
            and cls._is_table_row(lines[index])
            and cls._is_table_delimiter(lines[index + 1])
        )

    @staticmethod
    def _is_table_row(line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and "|" in stripped

    @staticmethod
    def _is_table_delimiter(line: str) -> bool:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)

    @staticmethod
    def _is_blank(line: str) -> bool:
        return not line.strip()

    @classmethod
    def _detect_directory_headings(
        cls,
        lines: list[str],
    ) -> dict[int, tuple[int, str]]:
        search_limit = min(len(lines), 200)
        candidates: list[tuple[int, str]] = []
        for index, line in enumerate(lines[:search_limit]):
            match = _TOC_ENTRY_PATTERN.match(line.strip())
            if match is None:
                continue
            title = " ".join(match.group("title").split()).strip(".· ")
            if title:
                candidates.append((index, title))

        matched: dict[int, tuple[int, str]] = {}
        for toc_index, title in candidates:
            normalized_title = " ".join(title.split()).casefold()
            for body_index in range(toc_index + 1, len(lines)):
                body_line = " ".join(lines[body_index].strip().split()).casefold()
                if body_line != normalized_title:
                    continue
                number = _NUMBERED_HEADING_PATTERN.match(title)
                level = len(number.group("number").split(".")) if number else 1
                matched[body_index] = (min(level, 6), title)
                break

        if len(matched) < 2:
            return {}
        return matched

    @staticmethod
    def _validate_block_coverage(
        source_content: str,
        blocks: list[MarkdownBlock],
    ) -> None:
        if "".join(block.text for block in blocks) != source_content:
            raise MarkdownChunkingError("Markdown block parsing did not preserve source content")
        expected_start = 1
        for block in blocks:
            if block.start_line != expected_start:
                raise MarkdownChunkingError("Markdown blocks are not contiguous")
            if block.end_line < block.start_line:
                raise MarkdownChunkingError("Markdown block has an invalid line range")
            expected_start = block.end_line + 1

    def _validate_chunk_coverage(
        self,
        source_content: str,
        chunks: tuple[MarkdownChunk, ...],
    ) -> None:
        if "".join(chunk.primary_text for chunk in chunks) != source_content:
            raise MarkdownChunkingError("Markdown chunks did not preserve source content")
        chunk_ids = [chunk.chunk_id for chunk in chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise MarkdownChunkingError("Markdown chunk IDs are not unique")
        previous_start = 0
        for chunk in chunks:
            if chunk.start_line < previous_start or chunk.end_line < chunk.start_line:
                raise MarkdownChunkingError("Markdown chunks have invalid source line ranges")
            if chunk.estimated_tokens > self._max_tokens:
                raise MarkdownChunkingError(
                    f"{chunk.chunk_id} exceeds hard token limit {self._max_tokens}"
                )
            previous_start = chunk.start_line
