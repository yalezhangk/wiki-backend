You extract source-grounded evidence from one chunk of a larger document.

Do not plan Wiki paths, create Wiki pages, patch existing Wiki content, or use knowledge that is not present in the primary source text.

Chunk metadata:
- chunk_id: ${chunk_id}
- primary source lines: ${source_line_range}
- section breadcrumbs: ${breadcrumbs}
- today's date: ${today}

Boundary overlap is context copied from the preceding chunk. It may clarify a sentence boundary, but it must not produce separate evidence and must not be cited as if it appeared inside the primary source lines:

=== OVERLAP CONTEXT START ===
${overlap_text}
=== OVERLAP CONTEXT END ===

Structural context may repeat a table header. It defines column meaning, but it is not a second occurrence of the evidence:

=== STRUCTURAL CONTEXT START ===
${structural_context}
=== STRUCTURAL CONTEXT END ===

Only the following primary text belongs to this chunk's source line range. The backend-added `L<number>:` prefix gives the exact absolute source line number. The prefix is metadata: do not copy it into evidence_text.

=== PRIMARY SOURCE START ===
${primary_text}
=== PRIMARY SOURCE END ===

Return only one valid JSON object. Do not include Markdown fences, explanations, apologies, or analysis.

For a successful extraction, return:

{
  "extraction_status": "succeeded",
  "extraction_error": null,
  "chunk_id": "${chunk_id}",
  "section_title": "the most specific section title, or document root",
  "summary": "a concise source-grounded summary; use a short low-information statement when appropriate",
  "evidence": [
    {
      "evidence_id": "${chunk_id}:e001",
      "statement": "one precise fact, specification, definition, requirement, relationship, or warning",
      "evidence_text": "a short verbatim excerpt from the primary source text",
      "source_lines": "line or inclusive line range inside ${source_line_range}",
      "category": "claim|specification|definition|requirement|relationship|warning"
    }
  ],
  "entities": ["source-named entities only"],
  "concepts": ["source-grounded concepts only"],
  "internal_conflicts": ["explicit conflicts inside this chunk only"]
}

Evidence requirements:
- Keep the response compact enough to finish: return at most 20 highest-value evidence items, at most 20 entries in each other list, a summary of at most 500 characters, statements of at most 300 characters, and evidence_text excerpts of at most 200 characters.
- Preserve model numbers, numeric values, units, conditions, exclusions, negations, warnings, and table-column meaning.
- Every evidence_text must be one short contiguous verbatim excerpt found within its declared primary source line or inclusive line range. Do not join separate excerpts with ellipses.
- Read source_lines directly from the `L<number>:` prefixes; do not infer or renumber lines.
- Evidence IDs must use ${chunk_id}:e001, ${chunk_id}:e002, and so on, without duplicates.
- Do not create evidence from overlap context or structural context alone.
- Repeated headers, footers, directory text, and marketing noise may be omitted.
- A low-information chunk is still a successful extraction and may return an empty evidence list.

If the chunk cannot be processed reliably, return only:

{
  "extraction_status": "failed",
  "extraction_error": "short reason",
  "chunk_id": "${chunk_id}"
}
