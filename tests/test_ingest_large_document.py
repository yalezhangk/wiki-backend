from __future__ import annotations

import unittest

from app.services.ingest_large_document import (
    MarkdownChunker,
    MarkdownChunkingError,
)
from app.services.ingest_large_document_status import LargeDocumentProgressEvent


class MarkdownChunkerTests(unittest.TestCase):
    def test_chunking_reports_start_and_completion(self) -> None:
        progress: list[LargeDocumentProgressEvent] = []

        chunks = MarkdownChunker().chunk(
            "# 产品\n\n正文。\n",
            progress_callback=progress.append,
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(
            [
                (
                    event.phase,
                    event.completed,
                    event.total,
                    event.item_id,
                    event.progress_percent,
                )
                for event in progress
            ],
            [
                ("chunking", 0, 1, None, 36),
                ("chunking", 1, 1, "chunks:1", 38),
            ],
        )

    def test_parse_blocks_preserves_markdown_structures_and_breadcrumbs(self) -> None:
        source = (
            "# 产品\n\n"
            "介绍段落。\n\n"
            "- 第一项\n"
            "- 第二项\n\n"
            "| 参数 | 值 |\n"
            "| --- | --- |\n"
            "| 电压 | 24kV |\n\n"
            "```text\n"
            "# 代码中的标题\n"
            "```\n"
        )

        blocks = MarkdownChunker().parse_blocks(source)

        self.assertEqual("".join(block.text for block in blocks), source)
        self.assertEqual(
            [block.kind for block in blocks],
            [
                "heading",
                "blank",
                "paragraph",
                "blank",
                "list",
                "blank",
                "table",
                "blank",
                "fenced_code",
            ],
        )
        self.assertEqual(blocks[2].heading_path, ("产品",))
        self.assertEqual(blocks[4].text, "- 第一项\n- 第二项\n")
        self.assertIn("# 代码中的标题", blocks[-1].text)

    def test_directory_entries_create_only_repeated_body_headings(self) -> None:
        source = (
            "目录\n"
            "概述 ...... 3\n"
            "安装与连接 ...... 8\n\n"
            "概述\n"
            "产品说明。\n\n"
            "安装与连接\n"
            "安装要求。\n"
        )

        blocks = MarkdownChunker().parse_blocks(source)
        synthetic_headings = [block for block in blocks if block.kind == "heading"]

        self.assertEqual([block.text.strip() for block in synthetic_headings], ["概述", "安装与连接"])
        self.assertEqual(synthetic_headings[0].heading_path, ("概述",))
        self.assertEqual(synthetic_headings[1].heading_path, ("安装与连接",))

    def test_nested_atx_headings_form_breadcrumbs(self) -> None:
        source = "# 产品\n## 安装\n### 电缆连接\n连接要求。\n## 维护\n维护要求。\n"

        blocks = MarkdownChunker().parse_blocks(source)
        paragraphs = [block for block in blocks if block.kind == "paragraph"]

        self.assertEqual(
            [block.heading_path for block in paragraphs],
            [
                ("产品", "安装", "电缆连接"),
                ("产品", "维护"),
            ],
        )

    def test_single_directory_like_line_is_not_treated_as_heading(self) -> None:
        source = "目录\n概述 ...... 3\n\n概述\n正文。\n"

        blocks = MarkdownChunker().parse_blocks(source)

        self.assertNotIn("heading", [block.kind for block in blocks])

    def test_heading_boundary_is_preferred_when_target_would_be_exceeded(self) -> None:
        source = "# 第一章\n" + "甲" * 8 + "\n# 第二章\n" + "乙" * 8 + "\n"
        chunker = MarkdownChunker(target_tokens=12, max_tokens=24, overlap_tokens=0)

        chunks = chunker.chunk(source)

        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].primary_text.startswith("# 第一章"))
        self.assertTrue(chunks[1].primary_text.startswith("# 第二章"))
        self.assertEqual("".join(chunk.primary_text for chunk in chunks), source)

    def test_table_and_fenced_code_are_not_split_when_within_hard_limit(self) -> None:
        table = "| 参数 | 值 |\n| --- | --- |\n| 电压 | 24kV |\n"
        fenced_code = "```text\n# 不是标题\n内容\n```\n"
        source = f"# 文档\n\n{table}\n{fenced_code}"
        chunker = MarkdownChunker(target_tokens=8, max_tokens=40, overlap_tokens=0)

        chunks = chunker.chunk(source)

        self.assertEqual(sum(table in chunk.primary_text for chunk in chunks), 1)
        self.assertEqual(sum(fenced_code in chunk.primary_text for chunk in chunks), 1)
        self.assertTrue(all(chunk.estimated_tokens <= 40 for chunk in chunks))

    def test_oversized_plain_paragraph_is_split_without_content_loss(self) -> None:
        source = "中" * 35
        chunker = MarkdownChunker(target_tokens=8, max_tokens=10, overlap_tokens=0)

        chunks = chunker.chunk(source)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunk.primary_text for chunk in chunks), source)
        self.assertTrue(all(chunk.estimated_tokens <= 10 for chunk in chunks))
        self.assertEqual({chunk.start_line for chunk in chunks}, {1})

    def test_overlap_is_explicit_and_not_counted_as_primary_coverage(self) -> None:
        source = "甲" * 8 + "\n\n" + "乙" * 8 + "\n"
        chunker = MarkdownChunker(target_tokens=8, max_tokens=14, overlap_tokens=3)

        chunks = chunker.chunk(source)

        self.assertEqual(len(chunks), 2)
        self.assertTrue(("甲" * 8 + "\n").endswith(chunks[1].overlap_text))
        self.assertLessEqual(len(chunks[1].overlap_text), 3)
        self.assertEqual("".join(chunk.primary_text for chunk in chunks), source)
        self.assertTrue(all(chunk.estimated_tokens <= 14 for chunk in chunks))

    def test_oversized_table_splits_only_between_rows_and_repeats_header_as_context(self) -> None:
        header = "| 列 | 值 |\n| --- | --- |\n"
        rows = [f"| {name} | {'值' * 5} |\n" for name in ("甲", "乙", "丙")]
        source = header + "".join(rows)
        chunker = MarkdownChunker(target_tokens=30, max_tokens=40, overlap_tokens=0)

        chunks = chunker.chunk(source)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunk.primary_text for chunk in chunks), source)
        self.assertEqual(chunks[0].structural_context, "")
        self.assertTrue(all(chunk.structural_context == header for chunk in chunks[1:]))
        for row in rows:
            self.assertEqual(sum(row in chunk.primary_text for chunk in chunks), 1)
        self.assertTrue(all(chunk.estimated_tokens <= 40 for chunk in chunks))

    def test_oversized_fenced_code_fails_with_line_range(self) -> None:
        source = "```text\n" + "中" * 20 + "\n```\n"
        chunker = MarkdownChunker(target_tokens=8, max_tokens=12, overlap_tokens=0)

        with self.assertRaisesRegex(
            MarkdownChunkingError,
            r"fenced_code block at lines 1-3 exceeds hard token limit 12",
        ):
            chunker.chunk(source)

    def test_chunk_ids_and_boundaries_are_stable(self) -> None:
        source = "# A\n\n" + "甲" * 12 + "\n\n# B\n\n" + "乙" * 12 + "\n"
        chunker = MarkdownChunker(target_tokens=10, max_tokens=18, overlap_tokens=2)

        first = chunker.chunk(source)
        second = chunker.chunk(source)

        self.assertEqual(first, second)
        self.assertEqual(len({chunk.chunk_id for chunk in first}), len(first))
        self.assertEqual(
            [(chunk.start_line, chunk.end_line) for chunk in first],
            sorted((chunk.start_line, chunk.end_line) for chunk in first),
        )

    def test_large_synthetic_manual_uses_default_limits_without_loss(self) -> None:
        section_names = (
            "概述",
            "产品系列说明",
            "功能与模块说明",
            "核心元件和附件",
            "数字化解决方案",
            "安装和连接",
        )
        source = "".join(
            f"# {section_name}\n{'中' * 8_000}\n"
            for section_name in section_names
        )

        chunks = MarkdownChunker().chunk(source)

        self.assertEqual(len(chunks), len(section_names))
        self.assertEqual("".join(chunk.primary_text for chunk in chunks), source)
        self.assertEqual(
            [chunk.breadcrumbs[0] for chunk in chunks],
            list(section_names),
        )
        self.assertTrue(all(chunk.estimated_tokens <= 16_000 for chunk in chunks))

    def test_invalid_limits_and_empty_source_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MarkdownChunker(target_tokens=10, max_tokens=9)
        with self.assertRaises(ValueError):
            MarkdownChunker(target_tokens=10, max_tokens=10, overlap_tokens=10)
        with self.assertRaisesRegex(MarkdownChunkingError, "cannot be empty"):
            MarkdownChunker().chunk("")


if __name__ == "__main__":
    unittest.main()
