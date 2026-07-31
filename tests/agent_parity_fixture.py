"""Health、Graph 与 Lint 的预核验 Agent parity fixture。"""
from __future__ import annotations

from pathlib import Path


EXPECTED_HEALTH = {
    "total_pages": 9,
    "empty_paths": ["wiki/drafts/empty.md", "wiki/drafts/short.md"],
    "stale_index_paths": ["wiki/sources/missing.md"],
    "missing_index_paths": [
        "wiki/concepts/conceptone.md",
        "wiki/drafts/empty.md",
        "wiki/drafts/short.md",
        "wiki/entities/entitytwo.md",
        "wiki/notes/deep/nested.md",
        "wiki/sources/beta.md",
    ],
    "unlogged_source_paths": ["wiki/sources/beta.md"],
}
EXPECTED_GRAPH = {
    "node_count": 9,
    "edge_pairs": {
        frozenset(("sources/alpha", "entities/entityone")),
        frozenset(("sources/beta", "entities/entityone")),
        frozenset(("entities/entitytwo", "concepts/conceptone")),
        frozenset(("entities/entitytwo", "notes/deep/nested")),
        frozenset(("concepts/conceptone", "notes/deep/nested")),
    },
    "phantom_hub": "missinghub",
}
EXPECTED_LINT = {
    "orphan_pages": {"sources/beta", "drafts/empty", "drafts/short"},
    "broken_link_pages": {"sources/alpha", "sources/beta", "entities/entityone"},
    "missing_entity": "missinghub",
    "sparse_pages": {"drafts/empty", "drafts/short"},
}


def create_agent_parity_wiki(root: Path) -> Path:
    """创建覆盖 Agent 三项巡检边界的最小 Wiki，不调用 Agent 或 LLM。"""
    wiki = root / "wiki"
    for relative in ("sources", "entities", "concepts", "notes/deep", "drafts"):
        (wiki / relative).mkdir(parents=True, exist_ok=True)
    (wiki / "index.md").write_text(
        "# Wiki Index\n\n- [Alpha](sources/alpha.md)\n- [Entity One](entities/entityone.md)\n"
        "- [Missing](sources/missing.md)\n",
        encoding="utf-8",
    )
    (wiki / "log.md").write_text(
        "# Wiki Log\n\n---\n\n## [2026-07-01] ingest | Alpha Source\n",
        encoding="utf-8",
    )
    _write_page(wiki / "sources/alpha.md", "source", "Alpha Source", "[[entityone]] [[missinghub]]", "Alpha evidence.")
    _write_page(wiki / "sources/beta.md", "source", "Beta Source", "[[entityone]] [[missinghub]]", "Beta evidence.")
    _write_page(wiki / "entities/entityone.md", "entity", "Entity One", "[[alpha]] [[missinghub]]", "Entity one evidence.")
    _write_page(wiki / "entities/entitytwo.md", "entity", "Entity Two", "[[conceptone]] [[nested]]", "Entity two evidence.")
    _write_page(wiki / "concepts/conceptone.md", "concept", "Concept One", "[[entitytwo]] [[nested]]", "Concept evidence.")
    _write_page(wiki / "notes/deep/nested.md", "unknown", "Nested", "[[conceptone]] [[entitytwo]]", "Nested evidence.")
    (wiki / "drafts/empty.md").write_text("---\ntitle: Empty\n---\n", encoding="utf-8")
    (wiki / "drafts/short.md").write_text("---\ntitle: Short\n---\n\nTiny.\n", encoding="utf-8")
    (wiki / "overview.md").write_text("# Overview\n\n" + "Overview context. " * 10, encoding="utf-8")
    return wiki


def _write_page(path: Path, page_type: str, title: str, links: str, evidence: str) -> None:
    path.write_text(
        f"---\ntype: {page_type}\ntitle: {title}\n---\n\n{links}\n\n" + evidence * 12,
        encoding="utf-8",
    )
