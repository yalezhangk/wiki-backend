"""共享 Wiki 文件分类，避免控制文件和运行产物进入知识页流程。"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

_KNOWLEDGE_DIRECTORIES = frozenset({"sources", "entities", "concepts", "syntheses"})
_OVERVIEW_PATH = Path("overview.md")


def is_knowledge_page(*, wiki_dir: Path, path: Path) -> bool:
    """判断文件是否是可参与巡检、图谱和问答的 Wiki 知识页。"""
    wiki_root = wiki_dir.resolve()
    try:
        resolved = path.resolve()
        relative = resolved.relative_to(wiki_root)
    except (OSError, ValueError):
        return False
    if not resolved.is_file() or resolved.suffix.lower() != ".md":
        return False
    if relative == _OVERVIEW_PATH:
        return True
    return len(relative.parts) >= 2 and relative.parts[0] in _KNOWLEDGE_DIRECTORIES


def iter_knowledge_pages(wiki_dir: Path) -> Iterator[Path]:
    """按稳定路径顺序返回受支持目录中的知识页。"""
    if not wiki_dir.is_dir():
        return
    candidates = [wiki_dir / _OVERVIEW_PATH]
    for directory in _KNOWLEDGE_DIRECTORIES:
        candidates.extend((wiki_dir / directory).rglob("*.md"))
    for path in sorted(candidates, key=lambda item: item.as_posix()):
        if is_knowledge_page(wiki_dir=wiki_dir, path=path):
            yield path
