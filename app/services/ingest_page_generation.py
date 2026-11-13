"""根据 Knowledge Plan 顺序生成 Source、知识页或受控 patch。"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import ValidationError

from app.config import settings
from app.llm_config import IngestModelProfile, LLMResponseTruncatedError
from app.prompts import render_prompt
from app.schemas.ingest_large_document import (
    IngestChunkEvidence,
    IngestChunkExtraction,
    IngestGeneratedKnowledgeCreate,
    IngestGeneratedKnowledgePatch,
    IngestGeneratedKnowledgeSkip,
    IngestGeneratedSourcePage,
    IngestKnowledgePlan,
    IngestPageGenerationFailure,
    IngestPlannedPage,
)
from app.services.ingest_context import (
    PromptBudget,
    TokenEstimator,
    WikiCandidate,
    WikiContextRetriever,
)
from app.services.ingest_plan_wiki_retrieval import KnowledgePlanWikiContext
from app.services.ingest_large_document_status import (
    LargeDocumentProgressCallback,
    LargeDocumentProgressEvent,
)
from app.services.ingest_result_assembly import (
    IngestResultAssemblyError,
    LargeDocumentResultAssembler,
    LargeDocumentResultAssembly as LargeDocumentPageGenerationResult,
)


LOGGER = logging.getLogger(__name__)

SOURCE_PAGE_MAX_OUTPUT_TOKENS = settings.ingest_stage_output_budgets.source_page
KNOWLEDGE_PAGE_MAX_OUTPUT_TOKENS = (
    settings.ingest_stage_output_budgets.knowledge_page
)
_WIKILINK_PATTERN = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_PROTECTED_PATCH_HEADINGS = frozenset({"related", "sources"})
_PAGE_DIRECTORIES = {"entity": "entities", "concept": "concepts"}


class PageGenerationLLMCaller(Protocol):
    """执行一次已带传输重试策略的 Ingest LLM 调用。"""

    def __call__(self, prompt: str, *, max_tokens: int) -> str: ...


class PageGenerationError(RuntimeError):
    """单个计划页面无法生成可信结果。"""

    def __init__(self, category: str, target_id: str, message: str) -> None:
        self.category = category
        self.target_id = target_id
        super().__init__(f"{category}: {target_id}: {message}")


class _InvalidPageJSON(ValueError):
    pass


class LargeDocumentPageGenerator:
    """先确定知识页动作，再使用实际链接目标生成 Source 并组装结果。"""

    def __init__(
        self,
        *,
        ingest_profile: IngestModelProfile,
        call_llm: PageGenerationLLMCaller,
        source_max_output_tokens: int = SOURCE_PAGE_MAX_OUTPUT_TOKENS,
        knowledge_max_output_tokens: int = KNOWLEDGE_PAGE_MAX_OUTPUT_TOKENS,
    ) -> None:
        if source_max_output_tokens <= 0 or knowledge_max_output_tokens <= 0:
            raise ValueError("output token limits must be positive")
        profile_limit = min(
            ingest_profile.capabilities.max_output_tokens,
            ingest_profile.llm_profile.max_tokens,
        )
        self._ingest_profile = ingest_profile
        self._call_llm = call_llm
        self._result_assembler = LargeDocumentResultAssembler()
        self._source_max_output_tokens = min(
            source_max_output_tokens,
            profile_limit,
        )
        self._knowledge_max_output_tokens = min(
            knowledge_max_output_tokens,
            profile_limit,
        )

    def generate(
        self,
        *,
        plan: IngestKnowledgePlan,
        extractions: tuple[IngestChunkExtraction, ...],
        wiki_context: KnowledgePlanWikiContext,
        today: date,
        job_id: int,
        progress_callback: LargeDocumentProgressCallback | None = None,
    ) -> LargeDocumentPageGenerationResult:
        """全部页面验证通过后才返回现有 `IngestLLMResult`。"""
        evidence_by_id, _ = self._index_evidence(extractions)
        source_plan = self._source_plan(plan)
        self._validate_page_evidence(
            source_plan,
            tuple(source_plan.supporting_evidence_ids),
            evidence_by_id,
        )
        knowledge_plans = [
            page for page in plan.planned_pages if page.page_type != "source"
        ]
        for page_plan in knowledge_plans:
            self._validate_page_evidence(
                page_plan,
                tuple(page_plan.supporting_evidence_ids),
                evidence_by_id,
            )
        page_total = len(knowledge_plans) + 1
        if progress_callback is not None:
            progress_callback(
                LargeDocumentProgressEvent(
                    phase="page_generation",
                    completed=0,
                    total=page_total,
                )
            )
        knowledge_results: list[
            IngestGeneratedKnowledgeCreate
            | IngestGeneratedKnowledgePatch
            | IngestGeneratedKnowledgeSkip
        ] = []
        for position, page_plan in enumerate(knowledge_plans, start=1):
            evidence_ids = tuple(page_plan.supporting_evidence_ids)
            relevant_candidates = self._relevant_candidates(page_plan, wiki_context)
            prompt = render_prompt(
                "ingest_knowledge_page.md",
                page_plan_json=self._json(page_plan.model_dump(mode="json")),
                evidence_json=self._json(
                    self._evidence_payload(evidence_ids, evidence_by_id)
                ),
                wiki_context=self._render_candidates(relevant_candidates),
                target_id=page_plan.path,
                page_type=page_plan.page_type,
            )
            LOGGER.info(
                "Knowledge page generation started job_id=%s position=%s total=%s "
                "target=%s wiki_sections=%s",
                job_id,
                position,
                len(knowledge_plans),
                page_plan.path,
                len(relevant_candidates),
            )
            result = self._request_knowledge(prompt, target_id=page_plan.path)
            generated = self._validate_knowledge_result(
                result,
                page_plan=page_plan,
                relevant_candidates=relevant_candidates,
                wiki_context=wiki_context,
            )
            knowledge_results.append(generated)
            LOGGER.info(
                "Knowledge page generation completed job_id=%s target=%s action=%s",
                job_id,
                page_plan.path,
                result.action,
            )
            if progress_callback is not None:
                progress_callback(
                    LargeDocumentProgressEvent(
                        phase="page_generation",
                        completed=position,
                        total=page_total,
                        item_id=page_plan.path,
                    )
                )

        allowed_link_targets = self._allowed_link_targets(
            plan,
            wiki_context,
            knowledge_results=knowledge_results,
        )
        for generated in knowledge_results:
            if isinstance(
                generated,
                (IngestGeneratedKnowledgeCreate, IngestGeneratedKnowledgePatch),
            ):
                self._validate_wikilinks(
                    generated.content,
                    allowed_targets=allowed_link_targets,
                    target_id=generated.target_id,
                )

        allowed_links = self._allowed_wiki_links(
            wiki_context,
            knowledge_results=knowledge_results,
        )
        source_evidence_ids = tuple(source_plan.supporting_evidence_ids)
        source_prompt = render_prompt(
            "ingest_source_page.md",
            document_plan_json=self._json(plan.model_dump(mode="json")),
            allowed_wiki_links="\n".join(allowed_links)
            or "(no Wiki links are allowed)",
            evidence_json=self._json(
                self._evidence_payload(source_evidence_ids, evidence_by_id)
            ),
            today=today.isoformat(),
            title=plan.title,
            slug=plan.slug,
        )
        LOGGER.info(
            "Large-document Source page generation started job_id=%s evidence=%s "
            "model=%s",
            job_id,
            len(source_evidence_ids),
            self._ingest_profile.model_identifier,
        )
        source_result = self._request_source(source_prompt)
        self._validate_source_result(
            source_result,
            plan=plan,
            today=today,
            allowed_link_targets=allowed_link_targets,
        )
        if progress_callback is not None:
            progress_callback(
                LargeDocumentProgressEvent(
                    phase="page_generation",
                    completed=page_total,
                    total=page_total,
                    item_id="source",
                )
            )

        try:
            assembled = self._result_assembler.assemble(
                plan=plan,
                source_result=source_result,
                knowledge_results=knowledge_results,
                selected_page_hashes=wiki_context.selected_page_hashes,
                today=today,
            )
        except IngestResultAssemblyError as exc:
            raise PageGenerationError(
                exc.category,
                exc.target_id,
                "分页面结果无法安全组装为现有 IngestLLMResult",
            ) from exc
        if progress_callback is not None:
            progress_callback(
                LargeDocumentProgressEvent(
                    phase="result_assembly",
                    completed=1,
                    total=1,
                    item_id="document",
                )
            )
        created_count = sum(
            isinstance(result, IngestGeneratedKnowledgeCreate)
            for result in knowledge_results
        )
        patch_count = sum(
            isinstance(result, IngestGeneratedKnowledgePatch)
            for result in knowledge_results
        )
        LOGGER.info(
            "Large-document page generation completed job_id=%s created=%s patches=%s "
            "skipped=%s",
            job_id,
            created_count,
            patch_count,
            len(knowledge_plans) - created_count - patch_count,
        )
        return assembled

    def _request_source(self, prompt: str) -> IngestGeneratedSourcePage:
        parsed = self._request_json(
            prompt,
            target_id="source",
            contract_name="Source page generation",
            max_output_tokens=self._source_max_output_tokens,
        )
        self._raise_model_failure(parsed, expected_target_id="source")
        try:
            return IngestGeneratedSourcePage.model_validate(parsed)
        except ValidationError as exc:
            raise PageGenerationError(
                "source_page_schema_invalid",
                "source",
                "Source 页面响应不符合生成合约",
            ) from exc

    def _request_knowledge(
        self,
        prompt: str,
        *,
        target_id: str,
    ) -> (
        IngestGeneratedKnowledgeCreate
        | IngestGeneratedKnowledgePatch
        | IngestGeneratedKnowledgeSkip
    ):
        parsed = self._request_json(
            prompt,
            target_id=target_id,
            contract_name="Entity or Concept page generation",
            max_output_tokens=self._knowledge_max_output_tokens,
        )
        self._raise_model_failure(parsed, expected_target_id=target_id)
        action = parsed.get("action")
        model = {
            "create": IngestGeneratedKnowledgeCreate,
            "patch": IngestGeneratedKnowledgePatch,
            "skip": IngestGeneratedKnowledgeSkip,
        }.get(action)
        if model is None:
            raise PageGenerationError(
                "knowledge_page_schema_invalid",
                target_id,
                "知识页响应缺少有效 action",
            )
        try:
            return model.model_validate(parsed)
        except ValidationError as exc:
            raise PageGenerationError(
                "knowledge_page_schema_invalid",
                target_id,
                "知识页响应不符合生成合约",
            ) from exc

    def _request_json(
        self,
        prompt: str,
        *,
        target_id: str,
        contract_name: str,
        max_output_tokens: int,
    ) -> dict[str, object]:
        raw = self._call_model(
            prompt,
            target_id=target_id,
            max_output_tokens=max_output_tokens,
        )
        try:
            return self._parse_json_object(raw)
        except _InvalidPageJSON:
            repair_prompt = render_prompt(
                "ingest_page_repair.md",
                contract_name=contract_name,
                target_id=target_id,
                invalid_response=raw,
            )
            repaired = self._call_model(
                repair_prompt,
                target_id=target_id,
                max_output_tokens=max_output_tokens,
            )
            try:
                return self._parse_json_object(repaired)
            except _InvalidPageJSON as exc:
                raise PageGenerationError(
                    "page_json_invalid",
                    target_id,
                    "页面响应在一次结构修复后仍不是有效 JSON 对象",
                ) from exc

    def _call_model(
        self,
        prompt: str,
        *,
        target_id: str,
        max_output_tokens: int,
    ) -> str:
        capabilities = self._ingest_profile.capabilities
        budget = PromptBudget(
            max_input_tokens=capabilities.max_input_tokens,
            max_output_tokens=max_output_tokens,
            safety_margin_tokens=capabilities.context_safety_margin_tokens,
            context_window_tokens=capabilities.context_window_tokens,
        )
        if not budget.accepts_input(TokenEstimator.estimate(prompt)):
            raise PageGenerationError(
                "page_prompt_too_large",
                target_id,
                "页面生成 Prompt 超出当前模型预算",
            )
        for attempt in range(2):
            try:
                return self._call_llm(prompt, max_tokens=max_output_tokens)
            except LLMResponseTruncatedError as exc:
                if attempt == 0:
                    LOGGER.warning(
                        "Page generation response truncated; retrying page once "
                        "target=%s",
                        target_id,
                    )
                    continue
                raise PageGenerationError(
                    "page_response_truncated",
                    target_id,
                    "页面响应重试一次后仍被截断",
                ) from exc
            except Exception as exc:
                raise PageGenerationError(
                    "page_llm_call_failed",
                    target_id,
                    "页面生成模型调用失败",
                ) from exc
        raise RuntimeError("unreachable page generation retry state")

    @staticmethod
    def _raise_model_failure(
        parsed: dict[str, object],
        *,
        expected_target_id: str,
    ) -> None:
        if parsed.get("generation_status") != "failed":
            return
        try:
            failure = IngestPageGenerationFailure.model_validate(parsed)
        except ValidationError as exc:
            raise PageGenerationError(
                "page_schema_invalid",
                expected_target_id,
                "页面失败响应不符合生成合约",
            ) from exc
        if failure.target_id != expected_target_id:
            raise PageGenerationError(
                "page_identity_mismatch",
                expected_target_id,
                "页面失败响应返回了错误的 target_id",
            )
        raise PageGenerationError(
            "page_generation_failed",
            expected_target_id,
            failure.generation_error,
        )

    def _validate_source_result(
        self,
        result: IngestGeneratedSourcePage,
        *,
        plan: IngestKnowledgePlan,
        today: date,
        allowed_link_targets: set[str],
    ) -> None:
        if result.title != plan.title or result.slug != plan.slug:
            raise PageGenerationError(
                "source_page_identity_mismatch",
                "source",
                "Source 页面 title 或 slug 与 Knowledge Plan 不一致",
            )
        fields = self._frontmatter_fields(result.content)
        title = self._unquote_yaml_scalar(fields.get("title", ""))
        page_type = self._unquote_yaml_scalar(fields.get("type", ""))
        page_date = self._unquote_yaml_scalar(fields.get("date", ""))
        if (
            title != plan.title
            or page_type != "source"
            or page_date != today.isoformat()
            or "tags" not in fields
        ):
            raise PageGenerationError(
                "source_frontmatter_invalid",
                "source",
                "Source 页面 Frontmatter 与计划不一致",
            )
        if "source_file" in fields or "source_url" in fields:
            raise PageGenerationError(
                "source_frontmatter_origin_forbidden",
                "source",
                "Source 来源字段必须由后端统一写入",
            )
        self._validate_wikilinks(
            result.content,
            allowed_targets=allowed_link_targets,
            target_id="source",
        )

    def _validate_knowledge_result(
        self,
        result: (
            IngestGeneratedKnowledgeCreate
            | IngestGeneratedKnowledgePatch
            | IngestGeneratedKnowledgeSkip
        ),
        *,
        page_plan: IngestPlannedPage,
        relevant_candidates: list[WikiCandidate],
        wiki_context: KnowledgePlanWikiContext,
    ) -> (
        IngestGeneratedKnowledgeCreate
        | IngestGeneratedKnowledgePatch
        | IngestGeneratedKnowledgeSkip
    ):
        if result.target_id != page_plan.path or result.page_type != page_plan.page_type:
            raise PageGenerationError(
                "knowledge_page_identity_mismatch",
                page_plan.path,
                "知识页响应 target_id 或 page_type 与计划不一致",
            )
        if isinstance(result, IngestGeneratedKnowledgeCreate):
            if result.title != page_plan.title or result.path != page_plan.path:
                raise PageGenerationError(
                    "knowledge_create_identity_mismatch",
                    page_plan.path,
                    "新页面 title 或 path 与计划不一致",
                )
            if result.path in wiki_context.selected_page_hashes:
                raise PageGenerationError(
                    "knowledge_create_existing_path",
                    page_plan.path,
                    "已检索存在的页面不能作为新页面生成",
                )
            self._validate_nonblank_content(result.content, target_id=page_plan.path)
        elif isinstance(result, IngestGeneratedKnowledgePatch):
            relevant = [
                candidate
                for candidate in relevant_candidates
                if candidate.path.removeprefix("wiki/") == result.path
            ]
            expected_hash = wiki_context.selected_page_hashes.get(result.path)
            if not relevant or expected_hash is None:
                raise PageGenerationError(
                    "knowledge_patch_path_unselected",
                    page_plan.path,
                    "patch 目标未出现在该页面调用的检索上下文中",
                )
            if result.base_hash != expected_hash:
                raise PageGenerationError(
                    "knowledge_patch_hash_mismatch",
                    page_plan.path,
                    "patch base_hash 与检索快照不一致",
                )
            expected_directory = _PAGE_DIRECTORIES[page_plan.page_type]
            if PurePosixPath(result.path).parts[:1] != (expected_directory,):
                raise PageGenerationError(
                    "knowledge_patch_path_invalid",
                    page_plan.path,
                    "patch 路径类型与页面计划不一致",
                )
            heading = result.heading.strip().lstrip("#").strip()
            if not heading or "\n" in heading or heading.casefold() in _PROTECTED_PATCH_HEADINGS:
                raise PageGenerationError(
                    "knowledge_patch_heading_invalid",
                    page_plan.path,
                    "patch heading 无效或属于受保护小节",
                )
            known_headings = {getattr(item, "heading", "").casefold() for item in relevant}
            if result.operation == "replace_section" and heading.casefold() not in known_headings:
                raise PageGenerationError(
                    "knowledge_patch_heading_unselected",
                    page_plan.path,
                    "replace_section 只能更新已检索到的小节",
                )
            if result.operation == "append_section" and heading.casefold() in known_headings:
                raise PageGenerationError(
                    "knowledge_patch_heading_exists",
                    page_plan.path,
                    "append_section 必须使用新小节标题",
                )
            self._validate_nonblank_content(result.content, target_id=page_plan.path)
        return result

    @staticmethod
    def _index_evidence(
        extractions: tuple[IngestChunkExtraction, ...],
    ) -> tuple[
        dict[str, tuple[IngestChunkExtraction, IngestChunkEvidence]],
        tuple[str, ...],
    ]:
        indexed: dict[str, tuple[IngestChunkExtraction, IngestChunkEvidence]] = {}
        order: list[str] = []
        for extraction in extractions:
            for evidence in extraction.evidence:
                if evidence.evidence_id in indexed:
                    raise PageGenerationError(
                        "page_input_evidence_duplicate",
                        evidence.evidence_id,
                        "输入 extraction 包含重复 evidence ID",
                    )
                indexed[evidence.evidence_id] = (extraction, evidence)
                order.append(evidence.evidence_id)
        if not indexed:
            raise PageGenerationError(
                "page_input_evidence_empty",
                "source",
                "分页面生成至少需要一条来源 evidence",
            )
        return indexed, tuple(order)

    @staticmethod
    def _evidence_payload(
        evidence_ids: tuple[str, ...],
        evidence_by_id: dict[
            str,
            tuple[IngestChunkExtraction, IngestChunkEvidence],
        ],
    ) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for evidence_id in evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if item is None:
                raise PageGenerationError(
                    "page_evidence_unknown",
                    evidence_id,
                    "页面计划引用了未知 evidence ID",
                )
            extraction, evidence = item
            payload.append(
                {
                    "chunk_id": extraction.chunk_id,
                    "section_title": extraction.section_title,
                    "evidence": evidence.model_dump(mode="json"),
                }
            )
        return payload

    @staticmethod
    def _validate_page_evidence(
        page_plan: IngestPlannedPage,
        evidence_ids: tuple[str, ...],
        evidence_by_id: dict[
            str,
            tuple[IngestChunkExtraction, IngestChunkEvidence],
        ],
    ) -> None:
        if len(evidence_ids) != len(set(evidence_ids)):
            raise PageGenerationError(
                "page_evidence_duplicate",
                page_plan.path,
                "页面计划包含重复 evidence ID",
            )
        owners: set[str] = set()
        for evidence_id in evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if item is None:
                raise PageGenerationError(
                    "page_evidence_unknown",
                    page_plan.path,
                    f"页面计划引用未知 evidence ID: {evidence_id}",
                )
            owners.add(item[0].chunk_id)
        if owners != set(page_plan.supporting_chunk_ids):
            raise PageGenerationError(
                "page_evidence_owner_mismatch",
                page_plan.path,
                "页面 evidence 与 supporting chunks 不一致",
            )

    @staticmethod
    def _source_plan(plan: IngestKnowledgePlan) -> IngestPlannedPage:
        source_pages = [page for page in plan.planned_pages if page.page_type == "source"]
        if len(source_pages) != 1:
            raise PageGenerationError(
                "source_plan_invalid",
                "source",
                "Knowledge Plan 必须且只能包含一个 Source 页面",
            )
        source_page = source_pages[0]
        if (
            source_page.title != plan.title
            or source_page.path != f"sources/{plan.slug}.md"
        ):
            raise PageGenerationError(
                "source_plan_invalid",
                "source",
                "Source 页面计划的 title 或 path 与文档计划不一致",
            )
        return source_page

    @staticmethod
    def _relevant_candidates(
        page_plan: IngestPlannedPage,
        wiki_context: KnowledgePlanWikiContext,
    ) -> list[WikiCandidate]:
        query = f"[[{page_plan.title}]]\n{page_plan.purpose}"
        expected_type = _PAGE_DIRECTORIES[page_plan.page_type]
        return [
            candidate
            for candidate in wiki_context.selected
            if candidate.page_type == expected_type
            and WikiContextRetriever.score_candidate(query, candidate) > 0
        ]

    @staticmethod
    def _allowed_wiki_links(
        wiki_context: KnowledgePlanWikiContext,
        *,
        knowledge_results: list[
            IngestGeneratedKnowledgeCreate
            | IngestGeneratedKnowledgePatch
            | IngestGeneratedKnowledgeSkip
        ],
    ) -> list[str]:
        allowed = [
            "[["
            f"{PurePosixPath(result.path).with_suffix('').as_posix()}|{result.title}"
            "]]"
            for result in knowledge_results
            if isinstance(result, IngestGeneratedKnowledgeCreate)
        ]
        allowed.extend(
            f"{candidate.path.removeprefix('wiki/')}#{candidate.heading}"
            for candidate in wiki_context.selected
        )
        return list(dict.fromkeys(allowed))

    @staticmethod
    def _render_candidates(candidates: list[WikiCandidate]) -> str:
        if not candidates:
            return "(no relevant existing Wiki evidence was retrieved)"
        return "\n\n---\n\n".join(
            "[path: "
            f"{candidate.path.removeprefix('wiki/')}; heading: {candidate.heading}; "
            f"snapshot: {candidate.snapshot_hash}]\n{candidate.content.strip()}"
            for candidate in candidates
        )

    @staticmethod
    def _allowed_link_targets(
        plan: IngestKnowledgePlan,
        wiki_context: KnowledgePlanWikiContext,
        *,
        knowledge_results: list[
            IngestGeneratedKnowledgeCreate
            | IngestGeneratedKnowledgePatch
            | IngestGeneratedKnowledgeSkip
        ],
    ) -> set[str]:
        targets = {
            plan.title.casefold(),
            f"sources/{plan.slug}".casefold(),
        }
        for result in knowledge_results:
            if isinstance(result, IngestGeneratedKnowledgeCreate):
                path = PurePosixPath(result.path)
                targets.add(result.title.casefold())
                targets.add(path.stem.casefold())
                targets.add(path.with_suffix("").as_posix().casefold())
        for candidate in wiki_context.selected:
            path = PurePosixPath(candidate.path.removeprefix("wiki/"))
            targets.add(path.stem.casefold())
            targets.add(path.with_suffix("").as_posix().casefold())
            targets.add(candidate.heading.casefold())
        return targets

    @staticmethod
    def _validate_wikilinks(
        content: str,
        *,
        allowed_targets: set[str],
        target_id: str,
    ) -> None:
        targets = {
            match.strip().casefold()
            for match in _WIKILINK_PATTERN.findall(content)
        }
        unknown = sorted(targets - allowed_targets)
        if unknown:
            raise PageGenerationError(
                "page_wikilink_unknown",
                target_id,
                f"页面引用了未检索或未计划的 Wikilink: {unknown}",
            )

    @staticmethod
    def _frontmatter_fields(content: str) -> dict[str, str]:
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            raise PageGenerationError(
                "source_frontmatter_invalid",
                "source",
                "Source 页面必须以 YAML Frontmatter 开始",
            )
        try:
            closing = next(
                index
                for index, line in enumerate(lines[1:], start=1)
                if line.strip() == "---"
            )
        except StopIteration as exc:
            raise PageGenerationError(
                "source_frontmatter_invalid",
                "source",
                "Source 页面缺少 Frontmatter 结束标记",
            ) from exc
        fields: dict[str, str] = {}
        collection_key: str | None = None
        for line in lines[1:closing]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[:1].isspace():
                if collection_key is None or not line.strip().startswith("-"):
                    raise PageGenerationError(
                        "source_frontmatter_invalid",
                        "source",
                        "Source 页面 Frontmatter 包含无法解析的嵌套字段",
                    )
                continue
            if ":" not in line:
                raise PageGenerationError(
                    "source_frontmatter_invalid",
                    "source",
                    "Source 页面 Frontmatter 包含无效字段",
                )
            key, value = line.split(":", 1)
            key = key.strip()
            if not key or key in fields:
                raise PageGenerationError(
                    "source_frontmatter_invalid",
                    "source",
                    "Source 页面 Frontmatter 包含空键或重复键",
                )
            fields[key] = value.strip()
            collection_key = key if not value.strip() else None
        return fields

    @staticmethod
    def _unquote_yaml_scalar(value: str) -> str:
        stripped = value.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {
            "'",
            '"',
        }:
            return stripped[1:-1]
        return stripped

    @staticmethod
    def _validate_nonblank_content(content: str, *, target_id: str) -> None:
        if not content.strip():
            raise PageGenerationError(
                "page_content_empty",
                target_id,
                "页面内容不能为空",
            )

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, object]:
        sanitized = raw.strip()
        if sanitized.startswith("```") and sanitized.endswith("```"):
            sanitized = re.sub(r"^```(?:json)?[ \t]*\r?\n?", "", sanitized)
            sanitized = re.sub(r"\r?\n?```$", "", sanitized).strip()
        try:
            parsed = json.loads(sanitized)
        except json.JSONDecodeError as exc:
            raise _InvalidPageJSON() from exc
        if not isinstance(parsed, dict):
            raise _InvalidPageJSON()
        return parsed
