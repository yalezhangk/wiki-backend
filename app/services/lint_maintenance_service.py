from __future__ import annotations

import hashlib
import json
import logging
import re
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, ValidationError

from app.llm_config import call_llm_main
from app.schemas.maintenance import MaintenanceJobResponse
from app.services.maintenance_service import MaintenanceTaskResult

_EXCLUDED = {"health-report.md", "index.md", "lint-report.md", "log.md"}
_CONFIDENCE_LABELS = {"high": 0.9, "medium": 0.6, "low": 0.3}
LOGGER = logging.getLogger(__name__)


class LintStorage(Protocol):
    def update_maintenance_job_progress(self, *, job_id: int, stage: str, progress_percent: int, updated_at: datetime) -> None: ...
    def upsert_maintenance_page_states(self, *, page_hashes: dict[str, str], checked_at: datetime) -> None: ...
    def replace_maintenance_findings(self, *, job_id: int, findings: list[dict[str, Any]], created_at: datetime) -> None: ...
    def get_maintenance_page_states(self) -> dict[str, dict[str, Any]]: ...
    def mark_maintenance_pages_semantically_checked(self, *, page_hashes: dict[str, str], job_id: int, checked_at: datetime) -> None: ...


class SemanticFinding(BaseModel):
    pages: list[str] = Field(min_length=1, max_length=6)
    evidence: str = Field(max_length=500)
    recommendation: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)


class SemanticResponse(BaseModel):
    contradictions: list[SemanticFinding] = Field(default_factory=list)
    stale_content: list[SemanticFinding] = Field(default_factory=list)
    data_gaps: list[SemanticFinding] = Field(default_factory=list)
    concepts_needing_depth: list[SemanticFinding] = Field(default_factory=list)


class SemanticResponseFormatError(ValueError):
    """模型未返回可验证 JSON 时，保留原始文本供报告和人工复核。"""

    def __init__(self, raw_response: str) -> None:
        self.raw_response = raw_response
        super().__init__("semantic response did not contain a JSON object")


class LintMaintenanceService:
    """执行确定性链接检查，并可受控调用主模型生成待人工复核的语义发现。"""
    def __init__(self, *, storage: LintStorage, wiki_repo_path: Path, wiki_lock: threading.RLock, max_pages: int = 20, max_chars: int = 24000) -> None:
        self._storage, self._root, self._wiki, self._lock = storage, wiki_repo_path.resolve(), wiki_repo_path.resolve() / "wiki", wiki_lock
        self._max_pages, self._max_chars = max_pages, max_chars

    def run(self, job: MaintenanceJobResponse) -> MaintenanceTaskResult:
        with self._lock:
            self._progress(job.job_id, "loading_wiki", 10)
            pages = [path for path in self._wiki.rglob("*.md") if path.name not in _EXCLUDED]
            content = {path: path.read_text(encoding="utf-8") for path in pages}
            hashes = {path.relative_to(self._wiki).as_posix(): hashlib.sha256(value.encode()).hexdigest() for path, value in content.items()}
            states = self._storage.get_maintenance_page_states()
            self._storage.upsert_maintenance_page_states(page_hashes=hashes, checked_at=self._now())
            self._progress(job.job_id, "checking_links", 30)
            findings = self._deterministic_findings(pages, content)
            self._progress(job.job_id, "checking_graph", 48)
            graph_status = self._append_agent_graph_findings(pages, content, findings)
            self._progress(job.job_id, "semantic_analysis", 55)
            semantic_failed = False
            semantic_unstructured = False
            selected: list[str] = []
            reasons: list[str] = []
            char_count = 0
            semantic_report = "Semantic analysis not run."
            if bool(job.options.get("semantic_analysis", True)):
                try:
                    if job.options.get("semantic_mode", "delta") == "agent_compat":
                        semantic_report, selected, char_count = self._agent_semantic_report(pages, content)
                        reasons = ["agent_compat:first-20-rglob-pages"] * len(selected)
                    else:
                        semantic_findings, selected, reasons, char_count = self._semantic(job, content, hashes, states, findings)
                        findings.extend(semantic_findings)
                        semantic_report = self._structured_semantic_report(semantic_findings)
                    self._storage.mark_maintenance_pages_semantically_checked(page_hashes={path: hashes[path] for path in selected}, job_id=job.job_id, checked_at=self._now())
                except SemanticResponseFormatError as exc:
                    LOGGER.warning(
                        "Lint semantic analysis returned an unstructured response for maintenance job %s; preserving it in the report",
                        job.job_id,
                    )
                    semantic_failed = True
                    semantic_unstructured = True
                    semantic_report = exc.raw_response.strip() or "Semantic analysis returned an empty unstructured response."
                except Exception as exc:
                    LOGGER.exception("Lint semantic analysis failed for maintenance job %s", job.job_id)
                    semantic_failed = True
                    semantic_report = "Semantic analysis unavailable."
            self._storage.replace_maintenance_findings(job_id=job.job_id, findings=findings, created_at=self._now())
            self._progress(job.job_id, "writing_report", 92)
            report = self._report(len(pages), findings, graph_status, semantic_report)
            (self._wiki / "lint-report.md").write_text(report, encoding="utf-8")
            self._prepend_log(job.job_id, semantic_failed)
        semantic_enabled = bool(job.options.get("semantic_analysis", True))
        coverage = {"candidate_count": len(selected) if semantic_enabled and not semantic_failed else 0, "checked_page_count": len(selected) if semantic_enabled and not semantic_failed else 0, "selection_reasons": reasons if semantic_enabled and not semantic_failed else [], "input_char_count": char_count if semantic_enabled and not semantic_failed else 0}
        semantic_audit = {
            "report_sha256": hashlib.sha256(semantic_report.encode("utf-8")).hexdigest(),
            "report_char_count": len(semantic_report),
        }
        semantic_status = (
            "unstructured"
            if semantic_unstructured
            else "unavailable"
            if semantic_failed
            else "not_run"
            if not semantic_enabled
            else "available"
        )
        structural_types = {"orphan", "sparse", "missing_entity", "broken_link"}
        graph_types = {"graph_orphan", "hub_stub", "fragile_bridge", "isolated_community"}
        return MaintenanceTaskResult(result_state="partial" if semantic_failed else "complete", result_summary={"structural_finding_count": len([item for item in findings if item["finding_type"] in structural_types]), "graph_link_orphan_count": len([item for item in findings if item["finding_type"] == "graph_orphan"]), "graph_aware_status": graph_status, "semantic_finding_count": len([item for item in findings if item["finding_type"] not in structural_types | graph_types]), "semantic_status": semantic_status, "semantic_coverage": coverage, "semantic_report_audit": semantic_audit, "report_name": "lint-report.md"})

    def _deterministic_findings(self, pages: list[Path], content: dict[Path, str]) -> list[dict[str, Any]]:
        by_stem: dict[str, list[Path]] = {}
        by_relative_path: dict[str, Path] = {}
        for path in pages:
            by_stem.setdefault(path.stem.lower(), []).append(path)
            relative_path = path.relative_to(self._wiki).with_suffix("").as_posix().lower()
            by_relative_path[relative_path] = path
        wiki_inbound = {path: 0 for path in pages}
        navigation_inbound = {path: 0 for path in pages}
        missing: dict[str, list[Path]] = {}
        sparse: list[tuple[Path, list[str]]] = []
        for path in pages:
            links = re.findall(r"\[\[([^\]]+)\]\]", content[path])
            for link in links:
                resolved = self._resolve_wikilink(link, by_stem, by_relative_path)
                if resolved:
                    for target in resolved:
                        wiki_inbound[target] += 1
                        navigation_inbound[target] += 1
                else:
                    missing.setdefault(link, []).append(path)
            unique_links = sorted(
                {
                    normalized
                    for link in links
                    if (normalized := self._wikilink_target(link).lower())
                }
            )
            if path.name != "overview.md" and len(unique_links) < 2:
                sparse.append((path, unique_links))
        navigation_documents = dict(content)
        index_path = self._wiki / "index.md"
        if index_path.is_file():
            navigation_documents[index_path] = index_path.read_text(encoding="utf-8")
        for source, markdown in navigation_documents.items():
            for target in self._resolve_markdown_links(source, markdown, set(pages)):
                navigation_inbound[target] += 1
        findings: list[dict[str, Any]] = []
        for link, sources in missing.items():
            source_ids = [self._page_id(path) for path in sources]
            findings.append(self._finding("broken_link", "warning", source_ids, f"修复或移除失效链接：{link}", 1.0, link))
            if len(sources) >= 3:
                findings.append(self._finding("missing_entity", "warning", source_ids, f"缺失实体候选：{link}", 1.0, link))
        for path, count in navigation_inbound.items():
            if path.name == "overview.md":
                continue
            if count == 0:
                findings.append(self._finding("orphan", "warning", [self._page_id(path)], "添加可解析的 WikiLink 或 Markdown 入链", 1.0))
            elif wiki_inbound[path] == 0:
                findings.append(self._finding("graph_orphan", "info", [self._page_id(path)], "页面可导航；如需纳入知识图谱，可添加入站 WikiLink", 1.0))
        for path, links in sparse:
            findings.append(self._finding("sparse", "info", [self._page_id(path)], f"增加出站 WikiLink（当前 {len(links)} 个）", 1.0, ", ".join(links)))
        return findings

    @staticmethod
    def _wikilink_target(link: str) -> str:
        return link.split("|", maxsplit=1)[0].split("#", maxsplit=1)[0].strip()

    def _resolve_wikilink(
        self,
        link: str,
        by_stem: dict[str, list[Path]],
        by_relative_path: dict[str, Path],
    ) -> list[Path]:
        target = self._wikilink_target(link)
        if not target:
            return []
        normalized_path = target.replace("\\", "/").strip("/")
        if normalized_path.lower().endswith(".md"):
            normalized_path = normalized_path[:-3]
        exact_path = by_relative_path.get(normalized_path.lower())
        if exact_path is not None:
            return [exact_path]
        return by_stem.get(Path(normalized_path).name.lower(), [])

    def _resolve_markdown_links(
        self,
        source: Path,
        markdown: str,
        pages: set[Path],
    ) -> set[Path]:
        resolved: set[Path] = set()
        for raw_target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", markdown):
            target = raw_target.strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path.lower().endswith(".md"):
                continue
            candidate = (source.parent / unquote(parsed.path)).resolve()
            try:
                candidate.relative_to(self._wiki)
            except ValueError:
                continue
            if candidate in pages:
                resolved.add(candidate)
        return resolved

    def _semantic(self, job: MaintenanceJobResponse, content: dict[Path, str], hashes: dict[str, str], states: dict[str, dict[str, Any]], findings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], list[str], int]:
        paths = {path.relative_to(self._wiki).as_posix(): path for path in content}
        risk_pages = {page for item in findings for page in item["affected_pages"]}
        mode = str(job.options.get("semantic_mode", "delta"))
        if mode == "selected": ordered = [path for path in job.options.get("selected_page_paths", []) if path in paths]
        else:
            changed = [path for path in sorted(paths) if states.get(path, {}).get("last_semantic_content_hash") != hashes[path]]
            risk = sorted(path for path in risk_pages if path in paths and path not in changed)
            remaining = sorted((path for path in paths if path not in changed and path not in risk), key=lambda path: str(states.get(path, {}).get("last_semantic_checked_at") or ""))
            ordered = risk + remaining if mode == "risk" else changed + risk + remaining
        selected: list[str] = []
        used = 0
        for relative in ordered:
            excerpt = content[paths[relative]][:1200]
            if len(selected) >= self._max_pages or used + len(excerpt) > self._max_chars: continue
            selected.append(relative); used += len(excerpt)
        output: list[dict[str, Any]] = []
        groups = self._comparison_groups(selected, paths, content)
        for group in groups:
            excerpts = [f"{relative}:\n{content[paths[relative]][:1200]}" for relative in group]
            prompt = (
                "Return JSON only with contradictions, stale_content, data_gaps, "
                "concepts_needing_depth arrays. Each item has pages, evidence, recommendation, "
                "confidence; pages must be a JSON array even when it has one path. Each confidence is "
                "a JSON number from 0.0 to 1.0; never use labels such as high, "
                "medium, or low. Never infer a cross-page contradiction from one page.\n"
                + "\n\n".join(excerpts)
            )
            try:
                parsed = self._parse_semantic_response(call_llm_main(prompt))
            except (ValidationError, json.JSONDecodeError) as exc:
                raise RuntimeError("semantic response validation failed") from exc
            for kind, items in (("contradiction", parsed.contradictions), ("stale_content", parsed.stale_content), ("data_gap", parsed.data_gaps), ("concept_depth", parsed.concepts_needing_depth)):
                for item in items:
                    valid_pages = [page for page in item.pages if page in group]
                    if valid_pages and not (kind == "contradiction" and len(valid_pages) < 2): output.append(self._finding(kind, "warning", valid_pages, item.recommendation, item.confidence, item.evidence))
        return output, selected, ([f"{mode}:group-{index + 1}" for index, group in enumerate(groups) for _ in group]), used

    @staticmethod
    def _parse_semantic_response(raw: str) -> SemanticResponse:
        """兼容常见置信度标签，再按结构化响应契约验证模型输出。"""
        payload = LintMaintenanceService._extract_json_payload(raw)
        if isinstance(payload, dict):
            for finding_type in (
                "contradictions",
                "stale_content",
                "data_gaps",
                "concepts_needing_depth",
            ):
                findings = payload.get(finding_type)
                if not isinstance(findings, list):
                    continue
                for finding in findings:
                    if not isinstance(finding, dict):
                        continue
                    confidence = finding.get("confidence")
                    if isinstance(confidence, str):
                        normalized = _CONFIDENCE_LABELS.get(confidence.strip().lower())
                        if normalized is not None:
                            finding["confidence"] = normalized
                    pages = finding.get("pages")
                    if isinstance(pages, str) and pages.strip():
                        finding["pages"] = [pages]
        return SemanticResponse.model_validate(payload)

    @staticmethod
    def _extract_json_payload(raw: str) -> Any:
        """从纯 JSON、Markdown JSON 代码块或说明文本中提取首个 JSON 值。"""
        candidates = [
            match.group(1)
            for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
        ]
        candidates.append(raw)
        decoder = json.JSONDecoder()
        for candidate in candidates:
            for start in [match.start() for match in re.finditer(r"[\[{]", candidate)]:
                try:
                    payload, _ = decoder.raw_decode(candidate[start:])
                except json.JSONDecodeError:
                    continue
                return payload
        raise SemanticResponseFormatError(raw)

    @staticmethod
    def _comparison_groups(selected: list[str], paths: dict[str, Path], content: dict[Path, str]) -> list[list[str]]:
        stem_map = {Path(relative).stem.lower(): relative for relative in selected}
        ungrouped = set(selected)
        groups: list[list[str]] = []
        for seed in selected:
            if seed not in ungrouped:
                continue
            group = [seed]
            ungrouped.remove(seed)
            for raw in re.findall(r"\[\[([^\]|#]+)", content[paths[seed]]):
                neighbor = stem_map.get(raw.strip().lower())
                if neighbor is not None and neighbor in ungrouped and len(group) < 6:
                    group.append(neighbor)
                    ungrouped.remove(neighbor)
            groups.append(group)
        return groups

    def _append_agent_graph_findings(self, pages: list[Path], content: dict[Path, str], findings: list[dict[str, Any]]) -> str:
        path = self._root / "graph" / "graph.json"
        if not path.is_file():
            return "skipped: graph unavailable"
        try:
            graph_data = json.loads(path.read_text(encoding="utf-8"))
            nodes = {node["id"]: node for node in graph_data.get("nodes", []) if isinstance(node, dict) and isinstance(node.get("id"), str)}
            edges = [edge for edge in graph_data.get("edges", []) if isinstance(edge, dict) and edge.get("from") in nodes and edge.get("to") in nodes]
            if not nodes or not edges:
                return "skipped: graph empty"
            degree = {node_id: 0 for node_id in nodes}
            for edge in edges: degree[edge["from"]] += 1; degree[edge["to"]] += 1
            values = list(degree.values())
            if len(values) >= 2:
                threshold = statistics.mean(values) + 2 * statistics.stdev(values)
                page_by_id = {self._page_id(page): page for page in pages}
                for node_id, value in degree.items():
                    page = page_by_id.get(node_id)
                    if value > threshold and page is not None and len(content[page]) < 500:
                        findings.append(self._finding("hub_stub", "warning", [node_id], "补充高连接页面内容与来源", 1.0, f"degree={value}; content_length={len(content[page])}"))
            communities = {node_id: node.get("group", -1) for node_id, node in nodes.items()}
            cross_community: dict[tuple[int, int], list[dict[str, Any]]] = {}
            external: set[int] = set()
            members: dict[int, list[str]] = {}
            for node_id, community in communities.items():
                if isinstance(community, int) and community >= 0:
                    members.setdefault(community, []).append(node_id)
            for edge in edges:
                first, second = communities.get(edge["from"], -1), communities.get(edge["to"], -1)
                if isinstance(first, int) and isinstance(second, int) and first >= 0 and second >= 0 and first != second:
                    pair = min(first, second), max(first, second)
                    cross_community.setdefault(pair, []).append(edge)
                    external.update((first, second))
            for pair, bridges in cross_community.items():
                if len(bridges) == 1:
                    edge = bridges[0]
                    findings.append(self._finding("fragile_bridge", "warning", [edge["from"], edge["to"]], f"增加社区 {pair[0]} 与 {pair[1]} 的替代连接", 1.0, f"communities={pair[0]}-{pair[1]}"))
            for community, node_ids in members.items():
                if len(node_ids) >= 2 and community not in external:
                    findings.append(self._finding("isolated_community", "info", node_ids[:10], f"社区 {community} 没有外部连接", 1.0, f"community={community}"))
        except (OSError, json.JSONDecodeError, TypeError, KeyError):
            return "skipped: graph unreadable"
        return "available"

    @staticmethod
    def _finding(kind: str, severity: str, pages: list[str], recommendation: str, confidence: float, evidence: str = "") -> dict[str, Any]:
        return {"finding_type": kind, "severity": severity, "affected_pages": pages, "evidence": [{"quote": evidence[:500]}] if evidence else [], "recommendation": recommendation[:1000], "confidence": max(0.0, min(1.0, confidence))}

    def _agent_semantic_report(self, pages: list[Path], content: dict[Path, str]) -> tuple[str, list[str], int]:
        sample = pages[:20]
        context = "".join(f"\n\n### {path.relative_to(self._root).as_posix()}\n{content[path][:1500]}" for path in sample)
        prompt = """You are linting an LLM Wiki. Review the pages below and identify:
1. Contradictions between pages (claims that conflict)
2. Stale content (summaries that newer sources have superseded)
3. Data gaps (important questions the wiki can't answer — suggest specific sources to find)
4. Concepts mentioned but lacking depth

Return a markdown lint report with these sections:
## Contradictions
## Stale Content
## Data Gaps & Suggested Sources
## Concepts Needing More Depth

Be specific — name the exact pages and claims involved.
""" + f"\nWiki pages (sample of {len(sample)} pages):{context}\n"
        return call_llm_main(prompt), [path.relative_to(self._wiki).as_posix() for path in sample], len(context)

    @staticmethod
    def _structured_semantic_report(findings: list[dict[str, Any]]) -> str:
        lines = ["## Contradictions", "", "## Stale Content", "", "## Data Gaps & Suggested Sources", "", "## Concepts Needing More Depth", ""]
        for finding in findings:
            lines.append(f"- {finding['finding_type']}: {', '.join(finding['affected_pages'])}")
        return "\n".join(lines)

    def _report(self, page_count: int, findings: list[dict[str, Any]], graph_status: str, semantic_report: str) -> str:
        """保持 Agent lint.py 的 Markdown 区块、提示和表格契约。"""
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for finding in findings:
            by_kind.setdefault(str(finding["finding_type"]), []).append(finding)
        orphans = by_kind.get("orphan", [])
        broken = by_kind.get("broken_link", [])
        missing_entities = by_kind.get("missing_entity", [])
        sparse = by_kind.get("sparse", [])
        graph_orphans = by_kind.get("graph_orphan", [])
        lines = [f"# Wiki Lint Report — {self._now().date().isoformat()}", "", f"Scanned {page_count} pages.", "", "## Structural Issues", ""]
        if orphans:
            lines.extend(["### Orphan Pages (no inbound links)", *[f"- `{self._wiki_path(item['affected_pages'][0])}`" for item in orphans], ""])
        if broken:
            lines.extend(["### Broken Wikilinks", *[f"- `{self._wiki_path(item['affected_pages'][0])}` links to `[[{self._evidence(item)}]]` — not found" for item in broken], ""])
        if missing_entities:
            lines.extend(["### Missing Entity Pages (mentioned 3+ times but no page)", "> [!warning] Action Required", "> Run `python3 generate_missing_entities.py` to automatically materialize these missing hubs."])
            lines.extend(f"- `[[{self._evidence(item)}]]`" for item in missing_entities)
            lines.append("")
        if sparse:
            lines.extend([f"### Sparse Pages — Low Outbound Link Density ({len(sparse)} pages)", "These pages have fewer than 2 outbound wikilinks. Add connections to prevent orphan accumulation:", "", "| Page | Outbound Links | Existing Links |", "|---|---|---|"])
            for item in sparse:
                evidence = self._evidence(item)
                links = ", ".join(f"`[[{link}]]`" for link in evidence.split(", ") if link) or "—"
                lines.append(f"| `{self._wiki_path(item['affected_pages'][0])}` | {len([link for link in evidence.split(', ') if link])} | {links} |")
            lines.append("")
        if not any((orphans, broken, missing_entities, sparse)):
            lines.extend(["No structural issues found.", ""])
        if graph_orphans:
            lines.extend(["### Graph-Link Orphans", f"{len(graph_orphans)} pages are reachable through Markdown navigation but have no inbound WikiLink. They are recorded as informational graph-coverage observations, not structural navigation failures.", ""])
        lines.extend(["## Graph-Aware Issues", ""])
        if graph_status == "skipped: graph unavailable":
            lines.extend(["> [!tip]", "> Graph-aware checks were skipped. Run `python tools/build_graph.py` first, then re-run lint.", ""])
        elif graph_status != "available":
            lines.extend(["> [!tip]", "> Graph data is empty. Ingest sources and run `python tools/build_graph.py` to populate.", ""])
        else:
            hubs = by_kind.get("hub_stub", [])
            lines.extend([f"### Hub Pages with Insufficient Content ({len(hubs)} pages)"])
            if hubs:
                lines.extend(["These hub nodes carry disproportionate connectivity but have thin content:", "", "| Page | Degree | Content Length | Status |", "|---|---|---|---|"])
                for item in hubs:
                    match = re.search(r"degree=(\d+); content_length=(\d+)", self._evidence(item))
                    degree, content_length = match.groups() if match else ("?", "?")
                    status = "🔴 stub" if content_length.isdigit() and int(content_length) < 250 else "🟡 thin"
                    lines.append(f"| `{self._wiki_path(item['affected_pages'][0])}` | {degree} | {content_length} chars | {status} |")
            else:
                lines.append("No hub stubs detected — all high-degree nodes have sufficient content.")
            bridges = by_kind.get("fragile_bridge", [])
            lines.extend(["", f"### Fragile Bridges ({len(bridges)} community pairs)"])
            if bridges:
                lines.append("These community connections rely on a single edge — one broken link isolates them:")
                for item in bridges:
                    match = re.search(r"communities=(\d+)-(\d+)", self._evidence(item))
                    first, second = match.groups() if match else ("?", "?")
                    lines.append(f"- Community {first} ↔ Community {second} via `{item['affected_pages'][0]}` → `{item['affected_pages'][1]}`")
            else:
                lines.append("No fragile bridges — all community connections have redundant links.")
            isolated = by_kind.get("isolated_community", [])
            lines.extend(["", f"### Isolated Communities ({len(isolated)} communities)"])
            if isolated:
                lines.extend(["These communities have zero external connections — knowledge silos:", "", "| Community | Nodes | Members |", "|---|---|---|"])
                for item in isolated:
                    community = self._evidence(item).removeprefix("community=")
                    members = ", ".join(item["affected_pages"][:5])
                    lines.append(f"| {community} | {len(item['affected_pages'])} | {members} |")
            else:
                lines.append("No isolated communities — all clusters have external connections.")
            lines.append("")
        lines.extend(["---", "", semantic_report.strip(), ""])
        return "\n".join(lines)

    @staticmethod
    def _evidence(finding: dict[str, Any]) -> str:
        evidence = finding.get("evidence", [])
        if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict):
            quote = evidence[0].get("quote", "")
            if isinstance(quote, str):
                return quote
        return "not recorded"

    @staticmethod
    def _wiki_path(page_id: str) -> str:
        return f"wiki/{page_id}.md"

    def _prepend_log(self, job_id: int, semantic_failed: bool) -> None:
        path = self._wiki / "log.md"
        existing = path.read_text(encoding="utf-8") if path.is_file() else "# Wiki Log\n\n"
        state = "partially completed" if semantic_failed else "completed"
        entry = f"## [{self._now().date().isoformat()}] lint | Wiki health check\n\nRan lint. See lint-report.md for details. Backend maintenance job {job_id} {state}.\n\n"
        separator = "---\n"
        position = existing.find(separator)
        if position >= 0:
            position += len(separator)
            updated = existing[:position] + "\n" + entry + existing[position:]
        else:
            updated = entry + existing
        path.write_text(updated, encoding="utf-8")

    def _page_id(self, path: Path) -> str:
        return path.relative_to(self._wiki).with_suffix("").as_posix()

    def _progress(self, job_id: int, stage: str, percent: int) -> None: self._storage.update_maintenance_job_progress(job_id=job_id, stage=stage, progress_percent=percent, updated_at=self._now())
    @staticmethod
    def _now() -> datetime: return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
