"""串行执行大文档 Ingest 的完整内存编排。"""
from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, TypeVar

from app.llm_config import IngestModelProfile
from app.schemas.ingest import IngestJobResponse
from app.services.ingest_chunk_extraction import ChunkEvidenceExtractor
from app.services.ingest_document_reduce import DocumentKnowledgeReducer
from app.services.ingest_large_document import MarkdownChunker
from app.services.ingest_large_document_status import (
    LargeDocumentLLMCaller,
    LargeDocumentPhase,
    LargeDocumentProgressCallback,
    normalize_large_document_error,
)
from app.services.ingest_page_generation import LargeDocumentPageGenerator
from app.services.ingest_plan_wiki_retrieval import KnowledgePlanWikiRetriever
from app.services.ingest_result_assembly import LargeDocumentResultAssembly


LOGGER = logging.getLogger(__name__)
_StageResult = TypeVar("_StageResult")


class LargeDocumentIngestOrchestrator:
    """在正式 Wiki 写入前完成所有大文档模型阶段和后验校验。"""

    def __init__(
        self,
        *,
        wiki_dir: Path,
        wiki_lock: AbstractContextManager[object],
        chunker: MarkdownChunker | None = None,
    ) -> None:
        self._wiki_dir = wiki_dir
        self._wiki_lock = wiki_lock
        self._chunker = chunker or MarkdownChunker()

    def ingest(
        self,
        *,
        job: IngestJobResponse,
        source: Path,
        source_content: str,
        ingest_profile: IngestModelProfile,
        call_llm: LargeDocumentLLMCaller,
        progress_callback: LargeDocumentProgressCallback,
    ) -> LargeDocumentResultAssembly:
        """依次执行完整管线；任一阶段失败时不产生正式 Wiki 写入。"""
        LOGGER.info(
            "Large-document ingest started job_id=%s source=%s model=%s",
            job.job_id,
            source.name,
            ingest_profile.model_identifier,
        )
        chunks = self._run_stage(
            "chunking",
            lambda: self._chunker.chunk(
                source_content,
                progress_callback=progress_callback,
            ),
        )
        extractions = self._run_stage(
            "chunk_extraction",
            lambda: ChunkEvidenceExtractor(
                ingest_profile=ingest_profile,
                call_llm=call_llm,
            ).extract(
                chunks,
                today=job.created_at.date(),
                progress_callback=progress_callback,
            ),
        )
        plan = self._run_stage(
            "document_reduce",
            lambda: DocumentKnowledgeReducer(
                ingest_profile=ingest_profile,
                call_llm=call_llm,
            ).reduce(
                extractions,
                document_filename=job.original_filename,
                today=job.created_at.date(),
                job_id=job.job_id,
                progress_callback=progress_callback,
            ),
        )
        wiki_context = self._run_stage(
            "wiki_retrieval",
            lambda: KnowledgePlanWikiRetriever(
                wiki_dir=self._wiki_dir,
                wiki_lock=self._wiki_lock,
            ).retrieve(
                plan,
                job_id=job.job_id,
                progress_callback=progress_callback,
            ),
        )
        try:
            result = LargeDocumentPageGenerator(
                ingest_profile=ingest_profile,
                call_llm=call_llm,
            ).generate(
                plan=plan,
                extractions=extractions,
                wiki_context=wiki_context,
                today=job.created_at.date(),
                job_id=job.job_id,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            category = getattr(exc, "category", "")
            phase: LargeDocumentPhase = (
                "result_assembly"
                if isinstance(category, str) and category.startswith(
                    ("assembly_", "assembled_")
                )
                else "page_generation"
            )
            raise normalize_large_document_error(
                phase=phase,
                error=exc,
            ) from exc
        LOGGER.info(
            "Large-document ingest completed job_id=%s chunks=%s pages=%s patches=%s",
            job.job_id,
            len(chunks),
            1 + len(result.data.entity_pages) + len(result.data.concept_pages),
            len(result.data.entity_patches) + len(result.data.concept_patches),
        )
        return result

    @staticmethod
    def _run_stage(
        phase: LargeDocumentPhase,
        operation: Callable[[], _StageResult],
    ) -> _StageResult:
        try:
            return operation()
        except Exception as exc:
            raise normalize_large_document_error(
                phase=phase,
                error=exc,
            ) from exc
