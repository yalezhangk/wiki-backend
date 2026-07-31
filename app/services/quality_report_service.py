from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.schemas.quality import (
    QualityCheckResponse,
    QualityCoverageResponse,
    QualityFindingResponse,
    QualityResponse,
    QualitySnapshotResponse,
)

LOGGER = logging.getLogger(__name__)
_EXCLUDED_FILENAMES = {"index.md", "log.md", "health-report.md", "lint-report.md"}
_LINT_FINDING_CATEGORIES = {
    "orphan": ("structure", "导航孤儿页面", "结构完整性"),
    "broken_link": ("structure", "失效 WikiLink", "结构完整性"),
    "missing_entity": ("structure", "缺失实体页", "结构完整性"),
    "sparse": ("structure", "低出链页面", "结构完整性"),
    "graph_orphan": ("graph", "仅 Markdown 可达页面", "图谱质量"),
    "hub_stub": ("graph", "高连接低内容页面", "图谱质量"),
    "fragile_bridge": ("graph", "脆弱社区连接", "图谱质量"),
    "isolated_community": ("graph", "孤立社区", "图谱质量"),
    "contradiction": ("consistency", "内容矛盾", "内容一致性"),
    "stale_content": ("consistency", "可能过时的内容", "内容一致性"),
    "data_gap": ("consistency", "内容数据缺口", "内容一致性"),
    "concept_depth": ("consistency", "概念深度不足", "内容一致性"),
}
_FINDING_SEVERITIES = {"critical", "warning", "info", "unknown"}
_FINDING_STATUSES = {"needs_review", "documented_difference", "unavailable"}


class QualityReportService:
    """只读解析最近一次维护报告；不会运行巡检或修复。"""

    def __init__(self, *, wiki_repo_path: Path, stale_after_hours: int, maintenance_storage: Any | None = None) -> None:
        self._root = wiki_repo_path.resolve()
        self._wiki_dir = self._root / "wiki"
        self._graph_dir = self._root / "graph"
        self._stale_after = timedelta(hours=stale_after_hours)
        self._maintenance_storage = maintenance_storage

    def get_latest(self) -> QualityResponse:
        if not self._wiki_dir.is_dir():
            raise RuntimeError("Wiki directory is unavailable")
        page_count = len([path for path in self._wiki_dir.rglob("*.md") if path.name not in _EXCLUDED_FILENAMES])
        checks = {
            "health": self._check(self._wiki_dir / "health-report.md", "结构检查"),
            "lint": self._check(self._wiki_dir / "lint-report.md", "语义巡检"),
            "graph": self._check(self._graph_dir / "graph-report.md", "图谱检查"),
            "freshness": QualityCheckResponse(state="not_run", message="尚无来源新鲜度快照"),
        }
        lint_job = self._latest_job("lint")
        graph_job = self._latest_job("graph")
        if lint_job is not None and lint_job.status == "succeeded" and lint_job.result_state == "partial":
            checks["lint"] = QualityCheckResponse(state="incomplete", generated_at=lint_job.finished_at, message="确定性 Lint 已完成，语义巡检不可用")
        if graph_job is not None and graph_job.status == "succeeded" and graph_job.result_state == "partial":
            checks["graph"] = QualityCheckResponse(state="incomplete", generated_at=graph_job.finished_at, message="确定性图谱已完成，部分图谱检查不可用")
        generated = max((item.generated_at for item in checks.values() if item.generated_at is not None), default=None)
        available = [item for name, item in checks.items() if name != "freshness" and item.state == "available"]
        snapshot_state = "available" if available else "missing"
        structural_checks = self._health_checks(self._wiki_dir / "health-report.md") if checks["health"].state == "available" else []
        graph_findings = self._graph_findings(self._graph_dir / "graph-report.md") if checks["graph"].state == "available" else []
        lint_findings = self._lint_findings(lint_job)
        structural_findings = lint_findings["structure"]
        consistency_findings = lint_findings["consistency"]
        graph_findings.extend(lint_findings["graph"])
        tab_counts = {"all": len(structural_findings) + len(consistency_findings) + len(graph_findings), "consistency": len(consistency_findings), "structure": len(structural_findings), "graph": len(graph_findings), "freshness": 0}
        semantic_coverage = lint_job.result_summary.get("semantic_coverage", {}) if lint_job is not None else {}
        checked_count = int(semantic_coverage.get("checked_page_count", 0)) if isinstance(semantic_coverage, dict) else 0
        return QualityResponse(
            snapshot=QualitySnapshotResponse(status=snapshot_state, generated_at=generated, current_object_count=page_count, coverage=QualityCoverageResponse(checked_object_count=checked_count, scope="sampled" if checked_count else "unknown"), checks=checks),
            tab_counts=tab_counts,
            structural={"checks": structural_checks, "findings": structural_findings},
            consistency={"findings": consistency_findings},
            graph={"findings": graph_findings},
            freshness={"recommendations": []},
        )

    def _latest_job(self, task_kind: str) -> Any | None:
        if self._maintenance_storage is None:
            return None
        try:
            jobs = self._maintenance_storage.list_maintenance_jobs(limit=1, task_kind=task_kind, workflow_id=None)
        except Exception:
            return None
        return jobs[0] if jobs else None

    def _check(self, path: Path, label: str) -> QualityCheckResponse:
        if not path.is_file():
            return QualityCheckResponse(state="missing", message=f"{label}报告缺失")
        try:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)
            if datetime.now(timezone.utc).replace(tzinfo=None) - modified_at > self._stale_after:
                return QualityCheckResponse(state="stale", generated_at=modified_at, message=f"{label}报告已过期")
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            LOGGER.warning("quality report is unreadable name=%s", path.name)
            return QualityCheckResponse(state="parse_failed", message=f"{label}报告无法解析")
        return QualityCheckResponse(state="available", generated_at=modified_at, message=f"{label}完成")

    @staticmethod
    def _health_checks(path: Path) -> list[dict[str, object]]:
        content = path.read_text(encoding="utf-8")
        return [
            {"label": "空页或短页", "state": "available", "count": QualityReportService._heading_count(content, "Empty / Stub Files"), "detail": "来自最近 Health 报告"},
            {"label": "索引同步", "state": "available", "count": QualityReportService._heading_count(content, "Index Sync"), "detail": "来自最近 Health 报告"},
            {"label": "入库日志覆盖", "state": "available", "count": QualityReportService._heading_count(content, "Log Coverage"), "detail": "来自最近 Health 报告"},
        ]

    @staticmethod
    def _heading_count(content: str, heading: str) -> int:
        match = re.search(rf"## {re.escape(heading)} \((\d+)", content)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _graph_findings(path: Path) -> list[QualityFindingResponse]:
        content = path.read_text(encoding="utf-8")
        result: list[QualityFindingResponse] = []
        for label, title in (("Orphan nodes", "孤儿节点"), ("Hub nodes", "Hub 节点")):
            match = re.search(rf"- {re.escape(label)}: (\d+)", content)
            if match is not None and int(match.group(1)) > 0:
                result.append(QualityFindingResponse(id=f"graph-{label.lower().replace(' ', '-')}", category="graph", severity="warning", status="needs_review", title=title, summary=f"最近图谱报告发现 {match.group(1)} 个{title}", report_section="Graph Report"))
        agent_orphans = re.search(r"## 🔴 Orphan Nodes \((\d+) pages(?:, [^)]+)?\)", content)
        if agent_orphans is not None and int(agent_orphans.group(1)) > 0:
            result.append(QualityFindingResponse(id="graph-orphan-nodes", category="graph", severity="warning", status="needs_review", title="孤儿节点", summary=f"最近图谱报告发现 {agent_orphans.group(1)} 个孤儿节点", report_section="Graph Insights Report"))
        phantom_hubs = re.search(r"## 🟠 Phantom Hubs \(referenced but non-existent pages\) \((\d+)\)", content)
        if phantom_hubs is not None and int(phantom_hubs.group(1)) > 0:
            result.append(QualityFindingResponse(id="graph-phantom-hubs", category="graph", severity="warning", status="needs_review", title="缺失的高频引用页", summary=f"最近图谱报告发现 {phantom_hubs.group(1)} 个 phantom hub", report_section="Graph Insights Report"))
        return result

    def _lint_findings(self, lint_job: Any | None) -> dict[str, list[QualityFindingResponse]]:
        grouped: dict[str, list[QualityFindingResponse]] = {"structure": [], "consistency": [], "graph": []}
        if (
            lint_job is None
            or lint_job.status != "succeeded"
            or self._maintenance_storage is None
        ):
            return grouped
        try:
            records = self._maintenance_storage.list_maintenance_findings(job_id=lint_job.job_id)
        except Exception:
            LOGGER.exception("quality snapshot could not load maintenance findings job_id=%s", lint_job.job_id)
            return grouped
        for record in records:
            finding_type = record.get("finding_type")
            if not isinstance(finding_type, str):
                continue
            mapping = _LINT_FINDING_CATEGORIES.get(finding_type)
            if mapping is None:
                LOGGER.warning("quality snapshot skipped unknown lint finding type=%s", finding_type)
                continue
            category, label, report_section = mapping
            pages = self._safe_pages(record.get("affected_pages"))
            page_summary = "、".join(pages[:3]) if pages else "未记录页面"
            recommendation = record.get("recommendation")
            recommendation_text = recommendation if isinstance(recommendation, str) and recommendation else None
            severity = record.get("severity")
            status = record.get("review_status")
            grouped[category].append(
                QualityFindingResponse(
                    id=f"lint-{lint_job.job_id}-{record.get('finding_id', 'unknown')}",
                    category=category,
                    severity=severity if severity in _FINDING_SEVERITIES else "unknown",
                    status=status if status in _FINDING_STATUSES else "needs_review",
                    title=f"{label}：{page_summary}",
                    summary=recommendation_text or "来自最新 Lint 任务的结构化发现，需人工核对。",
                    pages=pages,
                    evidence=self._safe_evidence(record.get("evidence")),
                    recommendation=recommendation_text,
                    report_section=report_section,
                )
            )
        return grouped

    @staticmethod
    def _safe_pages(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        pages: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            normalized = item.replace("\\", "/").strip()
            if (
                not normalized
                or normalized.startswith("/")
                or re.match(r"^[A-Za-z]:/", normalized)
                or ".." in normalized.split("/")
            ):
                continue
            pages.append(normalized)
        return pages

    @staticmethod
    def _safe_evidence(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        evidence: list[dict[str, str]] = []
        for item in value[:2]:
            if not isinstance(item, dict):
                continue
            quote = item.get("quote")
            if not isinstance(quote, str) or not quote.strip():
                continue
            evidence.append(
                {
                    "label": "巡检证据",
                    "source_label": "最新 Lint 任务",
                    "location": "maintenance_findings",
                    "quote": quote[:500],
                }
            )
        return evidence
