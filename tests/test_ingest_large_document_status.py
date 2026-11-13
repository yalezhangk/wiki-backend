from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.services.ingest_chunk_extraction import ChunkEvidenceExtractionError
from app.services.ingest_large_document_status import (
    LargeDocumentProgressEvent,
    normalize_large_document_error,
)


class LargeDocumentProgressEventTests(unittest.TestCase):
    def test_each_phase_stays_inside_extracting_progress_window(self) -> None:
        expected = {
            "chunking": (36, 38),
            "chunk_extraction": (39, 49),
            "document_reduce": (50, 54),
            "wiki_retrieval": (55, 57),
            "page_generation": (58, 63),
            "result_assembly": (64, 64),
        }

        for phase, (start, end) in expected.items():
            with self.subTest(phase=phase):
                started = LargeDocumentProgressEvent(
                    phase=phase,
                    completed=0,
                    total=4,
                )
                completed = LargeDocumentProgressEvent(
                    phase=phase,
                    completed=4,
                    total=4,
                )
                self.assertEqual(started.progress_percent, start)
                self.assertEqual(completed.progress_percent, end)

    def test_fractional_progress_is_deterministic_and_monotonic(self) -> None:
        values = [
            LargeDocumentProgressEvent(
                phase="chunk_extraction",
                completed=completed,
                total=4,
            ).progress_percent
            for completed in range(5)
        ]

        self.assertEqual(values, [39, 41, 44, 46, 49])
        self.assertEqual(values, sorted(values))

    def test_invalid_counts_and_blank_item_id_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            LargeDocumentProgressEvent(
                phase="document_reduce",
                completed=2,
                total=1,
            )
        with self.assertRaises(ValidationError):
            LargeDocumentProgressEvent(
                phase="document_reduce",
                completed=0,
                total=1,
                item_id="",
            )


class LargeDocumentStageErrorTests(unittest.TestCase):
    def test_known_error_keeps_category_and_safe_identifier(self) -> None:
        internal = ChunkEvidenceExtractionError(
            "chunk_json_invalid",
            "chunk-0001-safe",
            "raw model response must remain private",
        )

        normalized = normalize_large_document_error(
            phase="chunk_extraction",
            error=internal,
        )

        self.assertEqual(normalized.category, "chunk_json_invalid")
        self.assertEqual(normalized.item_id, "chunk-0001-safe")
        self.assertEqual(
            str(normalized),
            "chunk_json_invalid: 大文档的分块证据抽取失败。",
        )
        self.assertNotIn("raw model response", str(normalized))

    def test_unknown_or_invalid_category_uses_stable_fallback(self) -> None:
        internal = RuntimeError("full source content")

        normalized = normalize_large_document_error(
            phase="wiki_retrieval",
            error=internal,
            item_id="wiki-snapshot\nprivate-data",
        )

        self.assertEqual(
            normalized.category,
            "large_document_wiki_retrieval_failed",
        )
        self.assertEqual(normalized.item_id, "wiki-snapshot private-data")
        self.assertNotIn("full source content", str(normalized))


if __name__ == "__main__":
    unittest.main()
