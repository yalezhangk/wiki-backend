from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.wiki_page_policy import is_knowledge_page, iter_knowledge_pages


class WikiPagePolicyTests(unittest.TestCase):
    def test_only_contract_knowledge_pages_are_included(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            wiki = Path(temporary_directory) / "wiki"
            (wiki / "sources").mkdir(parents=True)
            (wiki / "syntheses").mkdir()
            (wiki / "notes").mkdir()
            for relative in (
                "overview.md",
                "sources/source.md",
                "syntheses/health-report.md",
                "index.md",
                "log.md",
                "health-report.md",
                "lint-report.md",
                "notes/draft.md",
            ):
                path = wiki / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("content", encoding="utf-8")

            pages = [path.relative_to(wiki).as_posix() for path in iter_knowledge_pages(wiki)]

            self.assertEqual(
                pages,
                ["overview.md", "sources/source.md", "syntheses/health-report.md"],
            )
            self.assertFalse(is_knowledge_page(wiki_dir=wiki, path=wiki / "health-report.md"))


if __name__ == "__main__":
    unittest.main()
