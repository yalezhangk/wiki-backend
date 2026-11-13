"""使用文档 Knowledge Plan 检索固定 Wiki 快照。"""
from __future__ import annotations

import logging
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.schemas.ingest_large_document import IngestKnowledgePlan
from app.services.ingest_context import (
    WIKI_CONTEXT_MAX_TOKENS,
    WikiCandidate,
    WikiContextRetriever,
    WikiSnapshot,
)
from app.services.ingest_large_document_status import (
    LargeDocumentProgressCallback,
    LargeDocumentProgressEvent,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class KnowledgePlanWikiContext:
    """从同一个 Wiki 快照选出的、可供后续 integration planning 使用的上下文。"""

    rendered_context: str
    selected: tuple[WikiCandidate, ...]
    skipped: tuple[str, ...]
    selected_page_hashes: dict[str, str]


class KnowledgePlanWikiRetriever:
    """只使用 Knowledge Plan 的稳定字段执行确定性词法检索。"""

    def __init__(
        self,
        *,
        wiki_dir: Path,
        wiki_lock: AbstractContextManager[object],
        max_context_tokens: int = WIKI_CONTEXT_MAX_TOKENS,
    ) -> None:
        if max_context_tokens < 0:
            raise ValueError("max_context_tokens must be non-negative")
        self._wiki_dir = wiki_dir
        self._wiki_lock = wiki_lock
        self._max_context_tokens = min(
            max_context_tokens,
            WIKI_CONTEXT_MAX_TOKENS,
        )

    def retrieve(
        self,
        plan: IngestKnowledgePlan,
        *,
        job_id: int,
        progress_callback: LargeDocumentProgressCallback | None = None,
    ) -> KnowledgePlanWikiContext:
        """在锁内拍摄快照，再从该不可变快照选择受限 Wiki section。"""
        if progress_callback is not None:
            progress_callback(
                LargeDocumentProgressEvent(
                    phase="wiki_retrieval",
                    completed=0,
                    total=1,
                    item_id="wiki-snapshot",
                )
            )
        query = self.build_query(plan)
        with self._wiki_lock:
            snapshot = WikiSnapshot.capture(self._wiki_dir)
        context_retriever = WikiContextRetriever(snapshot)
        candidates = context_retriever.candidates(query)
        preferred_paths = self._preferred_existing_paths(plan, candidates)
        selected, skipped = context_retriever.select(
            query,
            self._max_context_tokens,
            preferred_paths=preferred_paths,
        )
        selected_page_hashes = {
            item.path.removeprefix("wiki/"): item.snapshot_hash
            for item in selected
        }
        LOGGER.info(
            "Knowledge Plan Wiki context selected job_id=%s available_tokens=%s "
            "selected=%s skipped=%s",
            job_id,
            self._max_context_tokens,
            [
                (item.path, item.heading, item.score, item.snapshot_hash[:12])
                for item in selected
            ],
            skipped,
        )
        result = KnowledgePlanWikiContext(
            rendered_context=WikiContextRetriever.render(selected),
            selected=tuple(selected),
            skipped=tuple(skipped),
            selected_page_hashes=selected_page_hashes,
        )
        if progress_callback is not None:
            progress_callback(
                LargeDocumentProgressEvent(
                    phase="wiki_retrieval",
                    completed=1,
                    total=1,
                    item_id="wiki-snapshot",
                )
            )
        return result

    @staticmethod
    def _preferred_existing_paths(
        plan: IngestKnowledgePlan,
        candidates: list[WikiCandidate],
    ) -> set[str]:
        aliases_by_type: dict[str, set[str]] = {
            "entities": set(),
            "concepts": set(),
        }
        for page in plan.planned_pages:
            if page.page_type not in {"entity", "concept"}:
                continue
            directory = {
                "entity": "entities",
                "concept": "concepts",
            }[page.page_type]
            aliases_by_type[directory].update(
                {
                    KnowledgePlanWikiRetriever._normalize_alias(page.title),
                    KnowledgePlanWikiRetriever._normalize_alias(
                        PurePosixPath(page.path).stem
                    ),
                }
            )

        preferred: set[str] = set()
        for candidate in candidates:
            aliases = aliases_by_type.get(candidate.page_type, set())
            if not aliases:
                continue
            candidate_path = PurePosixPath(candidate.path.removeprefix("wiki/"))
            candidate_aliases = {
                KnowledgePlanWikiRetriever._normalize_alias(candidate_path.stem),
                KnowledgePlanWikiRetriever._normalize_alias(candidate.heading),
            }
            if aliases & candidate_aliases:
                preferred.add(candidate.path)
        return preferred

    @staticmethod
    def _normalize_alias(value: str) -> str:
        return re.sub(r"[\W_]+", "", value.casefold())

    @staticmethod
    def build_query(plan: IngestKnowledgePlan) -> str:
        """构造不含原始文档、完整 evidence 或 Wiki 内容的检索文本。"""
        lines = [f"Document title: {plan.title}"]
        claims = KnowledgePlanWikiRetriever._unique_text(plan.key_claims)
        if claims:
            lines.append("Key claims:")
            lines.extend(f"- {claim}" for claim in claims)

        knowledge_pages = [
            page
            for page in plan.planned_pages
            if page.page_type in {"entity", "concept"}
        ]
        if knowledge_pages:
            lines.append("Planned entities and concepts:")
            for page in knowledge_pages:
                lines.append(f"- [[{page.title}]] ({page.page_type})")

        purposes = KnowledgePlanWikiRetriever._unique_text(
            [page.purpose for page in plan.planned_pages]
        )
        if purposes:
            lines.append("Page purposes:")
            lines.extend(f"- {purpose}" for purpose in purposes)
        return "\n".join(lines)

    @staticmethod
    def _unique_text(values: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = " ".join(value.split())
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(normalized)
        return unique
