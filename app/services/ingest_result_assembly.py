"""将大文档逐页生成结果确定性组装为现有 Ingest 写入契约。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import Mapping, Sequence, TypeAlias

from pydantic import ValidationError

from app.schemas.ingest import (
    IngestGeneratedPage,
    IngestIndexEntry,
    IngestLLMResult,
    IngestPagePatch,
)
from app.schemas.ingest_large_document import (
    IngestGeneratedKnowledgeCreate,
    IngestGeneratedKnowledgePatch,
    IngestGeneratedKnowledgeSkip,
    IngestGeneratedSourcePage,
    IngestKnowledgePlan,
    IngestPlannedPage,
)


KnowledgeGenerationResult: TypeAlias = (
    IngestGeneratedKnowledgeCreate
    | IngestGeneratedKnowledgePatch
    | IngestGeneratedKnowledgeSkip
)
_PAGE_DIRECTORIES = {"entity": "entities", "concept": "concepts"}


class IngestResultAssemblyError(RuntimeError):
    """逐页结果无法安全转换为现有 IngestLLMResult。"""

    def __init__(self, category: str, target_id: str, message: str) -> None:
        self.category = category
        self.target_id = target_id
        super().__init__(f"{category}: {target_id}: {message}")


@dataclass(frozen=True)
class LargeDocumentResultAssembly:
    """可直接交给现有 Ingest 写入层的完整内存结果。"""

    data: IngestLLMResult
    selected_page_hashes: dict[str, str]


class LargeDocumentResultAssembler:
    """校验逐页结果的闭包关系，并确定性构造现有写入契约。"""

    def assemble(
        self,
        *,
        plan: IngestKnowledgePlan,
        source_result: IngestGeneratedSourcePage,
        knowledge_results: Sequence[KnowledgeGenerationResult],
        selected_page_hashes: Mapping[str, str],
        today: date,
    ) -> LargeDocumentResultAssembly:
        self._validate_source(plan, source_result)
        planned_pages = self._knowledge_plans(plan)
        generated_by_target = self._results_by_target(knowledge_results)
        if set(generated_by_target) != set(planned_pages):
            missing = sorted(set(planned_pages) - set(generated_by_target))
            unexpected = sorted(set(generated_by_target) - set(planned_pages))
            raise IngestResultAssemblyError(
                "assembly_plan_coverage_invalid",
                "document",
                f"逐页结果与计划不一致；missing={missing}, unexpected={unexpected}",
            )

        entity_pages: list[IngestGeneratedPage] = []
        concept_pages: list[IngestGeneratedPage] = []
        entity_entries: list[IngestIndexEntry] = []
        concept_entries: list[IngestIndexEntry] = []
        entity_patches: list[IngestPagePatch] = []
        concept_patches: list[IngestPagePatch] = []
        output_paths: set[str] = set()

        for target_id, page_plan in planned_pages.items():
            generated = generated_by_target[target_id]
            self._validate_result_identity(generated, page_plan)
            if isinstance(generated, IngestGeneratedKnowledgeCreate):
                self._append_create(
                    generated,
                    page_plan=page_plan,
                    selected_page_hashes=selected_page_hashes,
                    output_paths=output_paths,
                    entity_pages=entity_pages,
                    concept_pages=concept_pages,
                    entity_entries=entity_entries,
                    concept_entries=concept_entries,
                )
            elif isinstance(generated, IngestGeneratedKnowledgePatch):
                self._append_patch(
                    generated,
                    selected_page_hashes=selected_page_hashes,
                    output_paths=output_paths,
                    entity_patches=entity_patches,
                    concept_patches=concept_patches,
                )

        try:
            assembled = IngestLLMResult.model_validate(
                {
                    "ingest_status": "succeeded",
                    "ingest_error": None,
                    "title": plan.title,
                    "slug": plan.slug,
                    "source_page": source_result.content.strip(),
                    "index_entry": self._index_entry(
                        plan.title,
                        f"sources/{plan.slug}.md",
                        plan.document_summary,
                    ),
                    "entity_pages": entity_pages,
                    "concept_pages": concept_pages,
                    "entity_index_entries": entity_entries,
                    "concept_index_entries": concept_entries,
                    "entity_patches": entity_patches,
                    "concept_patches": concept_patches,
                    "contradictions": plan.contradictions,
                    "log_entry": self._log_entry(plan, today=today),
                }
            )
        except ValidationError as exc:
            raise IngestResultAssemblyError(
                "assembled_ingest_result_invalid",
                "document",
                "逐页结果无法组装为现有 IngestLLMResult",
            ) from exc
        return LargeDocumentResultAssembly(
            data=assembled,
            selected_page_hashes=dict(selected_page_hashes),
        )

    @staticmethod
    def _validate_source(
        plan: IngestKnowledgePlan,
        source_result: IngestGeneratedSourcePage,
    ) -> None:
        source_plans = [
            page for page in plan.planned_pages if page.page_type == "source"
        ]
        expected_path = f"sources/{plan.slug}.md"
        if (
            len(source_plans) != 1
            or source_plans[0].title != plan.title
            or source_plans[0].path != expected_path
        ):
            raise IngestResultAssemblyError(
                "assembly_source_plan_invalid",
                "source",
                "Knowledge Plan 必须包含唯一且与文档身份一致的 Source 页面",
            )
        if source_result.title != plan.title or source_result.slug != plan.slug:
            raise IngestResultAssemblyError(
                "assembly_source_identity_mismatch",
                "source",
                "Source 生成结果与文档计划身份不一致",
            )
        if not source_result.content.strip():
            raise IngestResultAssemblyError(
                "assembly_source_content_empty",
                "source",
                "Source 页面内容不能为空",
            )

    @staticmethod
    def _knowledge_plans(plan: IngestKnowledgePlan) -> dict[str, IngestPlannedPage]:
        planned: dict[str, IngestPlannedPage] = {}
        for page in plan.planned_pages:
            if page.page_type == "source":
                continue
            if page.path in planned:
                raise IngestResultAssemblyError(
                    "assembly_plan_target_duplicate",
                    page.path,
                    "Knowledge Plan 包含重复知识页目标",
                )
            planned[page.path] = page
        return planned

    @staticmethod
    def _results_by_target(
        knowledge_results: Sequence[KnowledgeGenerationResult],
    ) -> dict[str, KnowledgeGenerationResult]:
        indexed: dict[str, KnowledgeGenerationResult] = {}
        for generated in knowledge_results:
            if generated.target_id in indexed:
                raise IngestResultAssemblyError(
                    "assembly_result_target_duplicate",
                    generated.target_id,
                    "同一计划页面返回了多个生成结果",
                )
            indexed[generated.target_id] = generated
        return indexed

    @staticmethod
    def _validate_result_identity(
        generated: KnowledgeGenerationResult,
        page_plan: IngestPlannedPage,
    ) -> None:
        if generated.page_type != page_plan.page_type:
            raise IngestResultAssemblyError(
                "assembly_page_type_mismatch",
                generated.target_id,
                "逐页结果 page_type 与 Knowledge Plan 不一致",
            )

    def _append_create(
        self,
        generated: IngestGeneratedKnowledgeCreate,
        *,
        page_plan: IngestPlannedPage,
        selected_page_hashes: Mapping[str, str],
        output_paths: set[str],
        entity_pages: list[IngestGeneratedPage],
        concept_pages: list[IngestGeneratedPage],
        entity_entries: list[IngestIndexEntry],
        concept_entries: list[IngestIndexEntry],
    ) -> None:
        if generated.title != page_plan.title or generated.path != page_plan.path:
            raise IngestResultAssemblyError(
                "assembly_create_identity_mismatch",
                generated.target_id,
                "新页面 title 或 path 与 Knowledge Plan 不一致",
            )
        self._validate_output_path(
            generated.path,
            page_type=generated.page_type,
            target_id=generated.target_id,
        )
        if generated.path in selected_page_hashes:
            raise IngestResultAssemblyError(
                "assembly_create_existing_path",
                generated.target_id,
                "已存在于 Wiki 快照的路径不能作为新页面组装",
            )
        if not generated.content.strip():
            raise IngestResultAssemblyError(
                "assembly_page_content_empty",
                generated.target_id,
                "新页面内容不能为空",
            )
        self._claim_output_path(generated.path, output_paths)
        summary = " ".join(generated.index_summary.split())
        if not summary:
            raise IngestResultAssemblyError(
                "assembly_index_summary_empty",
                generated.target_id,
                "新页面 index summary 不能为空",
            )
        page = IngestGeneratedPage(
            path=generated.path,
            content=generated.content.strip(),
        )
        entry = IngestIndexEntry(
            path=generated.path,
            entry=self._index_entry(generated.title, generated.path, summary),
        )
        if generated.page_type == "entity":
            entity_pages.append(page)
            entity_entries.append(entry)
        else:
            concept_pages.append(page)
            concept_entries.append(entry)

    def _append_patch(
        self,
        generated: IngestGeneratedKnowledgePatch,
        *,
        selected_page_hashes: Mapping[str, str],
        output_paths: set[str],
        entity_patches: list[IngestPagePatch],
        concept_patches: list[IngestPagePatch],
    ) -> None:
        expected_hash = selected_page_hashes.get(generated.path)
        if expected_hash is None:
            raise IngestResultAssemblyError(
                "assembly_patch_path_unselected",
                generated.target_id,
                "patch 目标不在已检索 Wiki 快照中",
            )
        if generated.base_hash != expected_hash:
            raise IngestResultAssemblyError(
                "assembly_patch_hash_mismatch",
                generated.target_id,
                "patch base_hash 与已检索 Wiki 快照不一致",
            )
        self._validate_output_path(
            generated.path,
            page_type=generated.page_type,
            target_id=generated.target_id,
        )
        heading = generated.heading.strip().lstrip("#").strip()
        if not heading or not generated.content.strip():
            raise IngestResultAssemblyError(
                "assembly_patch_content_invalid",
                generated.target_id,
                "patch heading 和 content 不能为空",
            )
        self._claim_output_path(generated.path, output_paths)
        patch = IngestPagePatch(
            path=generated.path,
            base_hash=generated.base_hash,
            operation=generated.operation,
            heading=heading,
            content=generated.content.strip(),
        )
        if generated.page_type == "entity":
            entity_patches.append(patch)
        else:
            concept_patches.append(patch)

    @staticmethod
    def _claim_output_path(path: str, output_paths: set[str]) -> None:
        if path in output_paths:
            raise IngestResultAssemblyError(
                "assembly_output_path_duplicate",
                path,
                "多个逐页结果写入同一路径",
            )
        output_paths.add(path)

    @staticmethod
    def _validate_output_path(
        relative_path: str,
        *,
        page_type: str,
        target_id: str,
    ) -> None:
        path = PurePosixPath(relative_path)
        expected_directory = _PAGE_DIRECTORIES.get(page_type)
        if (
            expected_directory is None
            or path.is_absolute()
            or ".." in path.parts
            or path.parts[:1] != (expected_directory,)
            or path.suffix != ".md"
        ):
            raise IngestResultAssemblyError(
                "assembly_output_path_invalid",
                target_id,
                "页面路径不符合现有 Wiki 写入目录约束",
            )

    @staticmethod
    def _index_entry(title: str, path: str, summary: str) -> str:
        normalized_summary = " ".join(summary.split())
        return f"- [{title}]({path}) - {normalized_summary}"

    @staticmethod
    def _log_entry(plan: IngestKnowledgePlan, *, today: date) -> str:
        claims = "; ".join(" ".join(claim.split()) for claim in plan.key_claims)
        details = f"Added source. Key claims: {claims}" if claims else "Added source."
        return f"## [{today.isoformat()}] ingest | {plan.title}\n\n{details}"
