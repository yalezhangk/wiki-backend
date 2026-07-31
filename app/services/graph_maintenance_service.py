from __future__ import annotations

import hashlib
import json
import re
import statistics
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.llm_config import call_llm_fast
from app.schemas.maintenance import MaintenanceJobResponse
from app.services.graph_html_renderer import render_graph_html
from app.services.maintenance_service import MaintenanceTaskResult
from app.services.wiki_page_policy import iter_knowledge_pages

_TYPE_COLORS = {
    "source": "#4CAF50",
    "entity": "#2196F3",
    "concept": "#FF9800",
    "synthesis": "#9C27B0",
    "unknown": "#9E9E9E",
}
_EDGE_COLORS = {"EXTRACTED": "#555555", "INFERRED": "#FF5722", "AMBIGUOUS": "#BDBDBD"}
_COMMUNITY_COLORS = [
    "#E91E63", "#00BCD4", "#8BC34A", "#FF5722", "#673AB7",
    "#FFC107", "#009688", "#F44336", "#3F51B5", "#CDDC39",
]


class GraphMaintenanceStorage(Protocol):
    def update_maintenance_job_progress(
        self, *, job_id: int, stage: str, progress_percent: int, updated_at: datetime
    ) -> None:
        ...


class GraphMaintenanceService:
    """以 Agent 图谱产物契约构建并保存知识图谱。"""

    def __init__(
        self, *, storage: GraphMaintenanceStorage, wiki_repo_path: Path, wiki_lock: threading.RLock
    ) -> None:
        self._storage = storage
        self._repo_root = wiki_repo_path.resolve()
        self._wiki_dir = self._repo_root / "wiki"
        self._graph_dir = self._repo_root / "graph"
        self._wiki_lock = wiki_lock

    def run(self, job: MaintenanceJobResponse) -> MaintenanceTaskResult:
        with self._wiki_lock:
            self._progress(job.job_id, "reading_pages", 15)
            pages = self._pages()
            contents = {path: self._read_file(path) for path in pages}
            self._progress(job.job_id, "building_nodes", 30)
            nodes = [self._node(path, contents[path]) for path in pages]
            self._progress(job.job_id, "extracting_links", 45)
            edges = self._extracted_edges(pages, contents)
            inference_failed = False
            if bool(job.options.get("infer_relations", False)):
                self._progress(job.job_id, "inferring_relations", 60)
                try:
                    edges.extend(self._inferred_edges(pages, contents, edges))
                except Exception:
                    inference_failed = True
            edges = self._deduplicate_edges(edges)
            self._progress(job.job_id, "detecting_communities", 82)
            communities, communities_available = self._detect_communities(nodes, edges)
            self._apply_visual_properties(nodes, edges, communities)
            self._graph_dir.mkdir(parents=True, exist_ok=True)
            payload = {"nodes": nodes, "edges": edges, "built": date.today().isoformat()}
            self._progress(job.job_id, "writing_graph", 92)
            (self._graph_dir / "graph.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (self._graph_dir / "graph.html").write_text(
                self._html(nodes, edges), encoding="utf-8"
            )
            self._prepend_log(
                "graph",
                f"Knowledge graph rebuilt\n\n{len(nodes)} nodes, {len(edges)} edges "
                f"({sum(edge['type'] == 'EXTRACTED' for edge in edges)} extracted, "
                f"{sum(edge['type'] != 'EXTRACTED' for edge in edges)} inferred).",
            )
            save_report = bool(job.options.get("save_report", True))
            if save_report:
                report = self._report(nodes, edges, communities, pages, communities_available)
                (self._graph_dir / "graph-report.md").write_text(report, encoding="utf-8")
                self._prepend_log("report", f"Graph health report generated\n\n{len(nodes)} nodes analyzed.")

        result_state = "partial" if inference_failed or not communities_available else "complete"
        summary: dict[str, Any] = {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "extracted_edge_count": sum(edge["type"] == "EXTRACTED" for edge in edges),
            "inferred_edge_count": sum(edge["type"] != "EXTRACTED" for edge in edges),
            "community_count": len(set(communities.values())),
            "community_detection": "available" if communities_available else "unavailable",
            "report_name": "graph/graph-report.md" if save_report else None,
        }
        if inference_failed:
            summary["inference_status"] = "failed"
        return MaintenanceTaskResult(result_state=result_state, result_summary=summary)

    def _pages(self) -> list[Path]:
        if not self._wiki_dir.is_dir():
            raise RuntimeError("Wiki directory is unavailable")
        return list(iter_knowledge_pages(self._wiki_dir))

    def _node(self, path: Path, content: str) -> dict[str, Any]:
        page_type = self._frontmatter(content, "type") or "unknown"
        title = self._frontmatter(content, "title") or path.stem
        body = re.sub(r"^---\n.*?\n---\n?", "", content, flags=re.DOTALL)
        preview = " ".join(line.strip() for line in body.splitlines() if line.strip())[:220]
        return {
            "id": self._page_id(path),
            "label": title,
            "type": page_type,
            "color": _TYPE_COLORS.get(page_type, _TYPE_COLORS["unknown"]),
            "path": path.relative_to(self._repo_root).as_posix(),
            "markdown": content,
            "preview": preview,
        }

    def _extracted_edges(self, pages: list[Path], contents: dict[Path, str]) -> list[dict[str, Any]]:
        stem_map = {path.stem.lower(): self._page_id(path) for path in pages}
        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for path in pages:
            source = self._page_id(path)
            for link in set(re.findall(r"\[\[([^\]]+)\]\]", contents[path])):
                target = stem_map.get(link.lower())
                if target is not None and target != source and (source, target) not in seen:
                    seen.add((source, target))
                    edges.append(self._edge(source, target, "EXTRACTED", confidence=1.0))
        return edges

    def _inferred_edges(
        self, pages: list[Path], contents: dict[Path, str], existing_edges: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        cache_path = self._graph_dir / ".cache.json"
        checkpoint_path = self._graph_dir / ".inferred_edges.jsonl"
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
        checkpoint = self._load_checkpoint(checkpoint_path)
        page_ids = {self._page_id(path) for path in pages}
        node_list = "\n".join(f"- {self._page_id(path)} ({self._frontmatter(contents[path], 'type') or 'unknown'})" for path in pages)
        inferred: list[dict[str, Any]] = []
        for path in pages:
            source = self._page_id(path)
            content = contents[path]
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            cached = cache.get(source)
            relations: list[dict[str, Any]]
            if source in checkpoint:
                relations = checkpoint[source]
            elif isinstance(cached, dict) and cached.get("hash") == content_hash:
                relations = cached.get("edges", []) if isinstance(cached.get("edges"), list) else []
            else:
                extracted = "\n".join(
                    f"- {edge['from']} → {edge['to']} (EXTRACTED)"
                    for edge in existing_edges if edge["from"] == source
                )
                prompt = (
                    "Analyze this wiki page and return JSON only: "
                    '{"edges":[{"to":"page-id","relationship":"short","confidence":0.0,"type":"INFERRED"}]}.\n'
                    f"Source page: {source}\nContent:\n{content[:2000]}\n\n"
                    f"All available pages:\n{node_list}\n\nAlready-extracted edges:\n{extracted}\n"
                    "Only use existing page ids; do not repeat explicit edges."
                )
                parsed = self._parse_inference(call_llm_fast(prompt))
                relations = parsed
                cache[source] = {"hash": content_hash, "edges": relations}
                self._append_checkpoint(checkpoint_path, source, relations)
            for relation in relations:
                target = relation.get("to")
                confidence = relation.get("confidence", 0.7)
                if not isinstance(target, str) or target not in page_ids or target == source:
                    continue
                if not isinstance(confidence, (int, float)):
                    continue
                edge_type = relation.get("type")
                if edge_type not in {"INFERRED", "AMBIGUOUS"}:
                    edge_type = "INFERRED" if confidence >= 0.7 else "AMBIGUOUS"
                inferred.append(self._edge(source, target, edge_type, float(confidence), str(relation.get("relationship", ""))))
        self._graph_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        return inferred

    @staticmethod
    def _load_checkpoint(path: Path) -> dict[str, list[dict[str, Any]]]:
        if not path.is_file():
            return {}
        checkpoint: dict[str, list[dict[str, Any]]] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                page_id = record.get("page_id")
                edges = record.get("edges")
                if isinstance(page_id, str) and isinstance(edges, list):
                    checkpoint[page_id] = [edge for edge in edges if isinstance(edge, dict)]
        except (OSError, json.JSONDecodeError):
            return {}
        return checkpoint

    @staticmethod
    def _append_checkpoint(path: Path, page_id: str, edges: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"page_id": page_id, "edges": edges, "ts": date.today().isoformat()}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _parse_inference(raw: str) -> list[dict[str, Any]]:
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw.strip())
        parsed = json.loads(match.group(1) if match else raw)
        values = parsed.get("edges", []) if isinstance(parsed, dict) else parsed
        return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []

    @staticmethod
    def _edge(source: str, target: str, edge_type: str, confidence: float, title: str = "") -> dict[str, Any]:
        return {"id": f"{source}->{target}:{edge_type}", "from": source, "to": target, "type": edge_type, "title": title, "label": "", "color": _EDGE_COLORS[edge_type], "confidence": confidence}

    @staticmethod
    def _deduplicate_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for edge in edges:
            key = tuple(sorted((str(edge["from"]), str(edge["to"]))))
            if key not in best or float(edge.get("confidence", 0)) > float(best[key].get("confidence", 0)):
                best[key] = edge
        return list(best.values())

    @staticmethod
    def _detect_communities(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> tuple[dict[str, int], bool]:
        try:
            import networkx as nx
        except ImportError:
            return {}, False
        graph = nx.Graph()
        graph.add_nodes_from(node["id"] for node in nodes)
        graph.add_edges_from((edge["from"], edge["to"]) for edge in edges)
        if graph.number_of_edges() == 0:
            return {}, True
        try:
            return {
                node_id: index
                for index, community in enumerate(nx.community.louvain_communities(graph, seed=42))
                for node_id in community
            }, True
        except Exception:
            return {}, False

    @staticmethod
    def _apply_visual_properties(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], communities: dict[str, int]) -> None:
        degree: dict[str, int] = {}
        for edge in edges:
            degree[edge["from"]] = degree.get(edge["from"], 0) + 1
            degree[edge["to"]] = degree.get(edge["to"], 0) + 1
        for node in nodes:
            group = communities.get(node["id"], -1)
            node["group"] = group
            if group >= 0:
                node["color"] = _COMMUNITY_COLORS[group % len(_COMMUNITY_COLORS)]
            node["value"] = degree.get(node["id"], 0) + 1

    def _report(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], communities: dict[str, int], pages: list[Path], communities_available: bool) -> str:
        degree = {node["id"]: 0 for node in nodes}
        for edge in edges:
            degree[edge["from"]] += 1
            degree[edge["to"]] += 1
        orphans = sorted(node_id for node_id, value in degree.items() if value == 0)
        degree_values = list(degree.values())
        threshold = (
            statistics.mean(degree_values)
            + (2 * statistics.stdev(degree_values) if len(degree_values) > 1 else 0)
            if degree_values
            else 0
        )
        god_nodes = sorted(((node_id, value) for node_id, value in degree.items() if value > threshold), key=lambda item: item[1], reverse=True)
        community_members: dict[int, list[str]] = {}
        for node_id, group in communities.items():
            community_members.setdefault(group, []).append(node_id)
        cross_community: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for edge in edges:
            first, second = communities.get(edge["from"], -1), communities.get(edge["to"], -1)
            if first >= 0 and second >= 0 and first != second:
                cross_community.setdefault((min(first, second), max(first, second)), []).append(edge)
        fragile_bridges = [(pair, values[0]) for pair, values in sorted(cross_community.items()) if len(values) == 1]
        phantoms = self._phantom_hubs(pages)
        edge_density = (2 * len(edges) / (len(nodes) * (len(nodes) - 1))) if len(nodes) > 1 else 0
        orphan_pct = (len(orphans) / len(nodes) * 100) if nodes else 0
        edges_per_node = len(edges) / len(nodes) if nodes else 0
        health = "✅ healthy" if edges_per_node >= 2 else "⚠️ warning" if edges_per_node >= 1 else "🔴 critical"
        lines = [f"# Graph Insights Report — {date.today().isoformat()}", "", "## Health Summary", f"- **{len(nodes)}** nodes, **{len(edges)}** edges ({edges_per_node:.2f} edges/node — {health})", f"- **{len(orphans)}** orphan nodes ({orphan_pct:.1f}%) — target: <10%", f"- **{len(community_members)}** communities", f"- Link density: {edge_density:.4f}", "", f"## 🔴 Orphan Nodes ({len(orphans)} pages, {orphan_pct:.1f}%)"]
        lines.extend((f"- `{node_id}`" for node_id in orphans) if orphans else ["No orphan nodes — excellent!"])
        lines.extend(["", "## 🟡 God Nodes (Hub Pages)"])
        if god_nodes:
            lines.extend(["| Node | Degree | % of Edges | Community |", "|---|---|---|---|"])
            for node_id, value in god_nodes:
                lines.append(f"| `{node_id}` | {value} | {(value / (2 * len(edges)) * 100) if edges else 0:.1f}% | {communities.get(node_id, -1)} |")
        else:
            lines.append("No god nodes detected — degree distribution is balanced.")
        lines.extend(["", "## 🟡 Fragile Bridges"])
        lines.extend((f"- Community {pair[0]} ↔ Community {pair[1]} via `{edge['from']}` → `{edge['to']}`" for pair, edge in fragile_bridges) if fragile_bridges else ["No fragile bridges — all community connections are redundant."])
        lines.extend(["", "## 🟢 Community Overview"])
        if community_members:
            lines.extend(["", "| Community | Nodes | Key Members |", "|---|---|---|"])
            for group, members in sorted(community_members.items()):
                key_members = sorted(members, key=lambda node_id: degree.get(node_id, 0), reverse=True)[:5]
                lines.append(f"| {group} | {len(members)} | {', '.join(key_members)} |")
        else:
            lines.append("No communities detected.")
        lines.extend(["", f"## 🟠 Phantom Hubs (referenced but non-existent pages) ({len(phantoms)})"])
        if phantoms:
            lines.extend(["| Page Name | References | Referenced By |", "|---|---|---|"])
            for phantom in phantoms:
                lines.append(f"| `[[{phantom['name']}]]` | {phantom['ref_count']} | {', '.join(phantom['referenced_by'][:3])} |")
        else:
            lines.append("No phantom hubs — all referenced pages exist.")
        actions: list[str] = []
        if orphans:
            actions.append(f"Add wikilinks to top orphan pages (highest potential impact: {orphans[0]})")
        if god_nodes:
            actions.append("Review god nodes for stub content vs. genuine hubs")
        if fragile_bridges:
            actions.append("Strengthen fragile bridges with cross-references")
        if phantoms:
            actions.append(f"Create pages for top phantom hubs (start with `[[{phantoms[0]['name']}]]`)")
        lines.extend(["", "## Suggested Actions"])
        lines.extend((f"{index}. {action}" for index, action in enumerate(actions, start=1)) if actions else ["1. Graph is in good shape — maintain current linking practices"])
        if not communities_available:
            lines.extend(["", "Community detection was unavailable; graph findings are partial."])
        return "\n".join(lines) + "\n"

    def _phantom_hubs(self, pages: list[Path]) -> list[dict[str, Any]]:
        existing = {path.stem.lower() for path in pages}
        references: dict[str, set[str]] = {}
        for path in pages:
            for link in set(re.findall(r"\[\[([^\]]+)\]\]", self._read_file(path))):
                if link.lower() not in existing:
                    references.setdefault(link, set()).add(self._page_id(path))
        return sorted(({"name": name, "ref_count": len(source), "referenced_by": sorted(source)} for name, source in references.items() if len(source) >= 2), key=lambda item: int(item["ref_count"]), reverse=True)

    @staticmethod
    def _html(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
        """渲染与 Agent render_html() 交互行为对齐的独立图谱页面。"""
        return render_graph_html(nodes, edges)

    def _prepend_log(self, kind: str, detail: str) -> None:
        path = self._wiki_dir / "log.md"
        existing = self._read_file(path) or "# Wiki Log\n\n"
        entry = f"## [{date.today().isoformat()}] {kind} | {detail.strip()}"
        separator = "---\n"
        position = existing.find(separator)
        updated = existing[: position + len(separator)] + "\n" + entry + "\n\n" + existing[position + len(separator):] if position >= 0 else entry + "\n\n" + existing
        path.write_text(updated.rstrip() + "\n", encoding="utf-8")

    @staticmethod
    def _frontmatter(content: str, field: str) -> str:
        match = re.search(rf"^{re.escape(field)}:\s*[\"']?([^\"'\n]+)", content, re.MULTILINE)
        return match.group(1).strip() if match else ""

    def _page_id(self, path: Path) -> str:
        return path.relative_to(self._wiki_dir).with_suffix("").as_posix()

    @staticmethod
    def _read_file(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _progress(self, job_id: int, stage: str, percent: int) -> None:
        self._storage.update_maintenance_job_progress(job_id=job_id, stage=stage, progress_percent=percent, updated_at=datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0))
