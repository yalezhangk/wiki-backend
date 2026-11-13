Create one document-level knowledge plan from the structured source evidence below.

Do not use existing Wiki content, guess existing Wiki paths, generate complete pages, or add facts that are absent from the structured evidence. Merge duplicate facts and synonymous entity or concept names while preserving model numbers, numeric values, units, applicability conditions, exclusions, negations, and explicit conflicts.

Document metadata:
- filename: ${document_filename}
- today's date: ${today}
- input representation: ${reduce_input_type}

The input contains every source chunk, either directly as chunk extractions or through validated section syntheses:

=== STRUCTURED REDUCE INPUT START ===
${reduce_items_json}
=== STRUCTURED REDUCE INPUT END ===

Return only one valid JSON object with this shape:

{
  "planning_status": "succeeded",
  "planning_error": null,
  "title": "one title for the complete document",
  "slug": "lowercase-kebab-case",
  "document_summary": "concise document-level summary",
  "key_claims": ["deduplicated source-grounded claims"],
  "covered_chunk_ids": ["every input chunk ID exactly once"],
  "planned_pages": [
    {
      "page_type": "source|entity|concept",
      "title": "page title",
      "path": "sources/<slug>.md|entities/<slug>.md|concepts/<slug>.md",
      "purpose": "why this page has cross-source knowledge value",
      "supporting_chunk_ids": ["existing chunk IDs"],
      "supporting_evidence_ids": ["existing evidence IDs"]
    }
  ],
  "patch_intents": [],
  "contradictions": ["explicit internal conflicts only"]
}

Planning requirements:
- Keep the plan bounded: at most 20 key_claims, at most 6 planned_pages including the Source page, at most 20 contradictions, a document_summary of at most 1200 characters, and at most 20 supporting evidence IDs per page.
- covered_chunk_ids must contain every represented chunk exactly once, including low-information chunks.
- Plan exactly one source page at sources/<document slug>.md.
- Plan Entity or Concept pages only for reusable knowledge objects, not for every heading.
- Do not plan duplicate pages for spelling, capitalization, or alias variations of the same object.
- Every planned page must cite at least one existing chunk and evidence ID. Each cited evidence ID must belong to one of that page's supporting chunks.
- Keep patch_intents empty. Existing Wiki content has not been retrieved yet, so existing paths cannot be known at this stage.
- Preserve explicit conflicts without guessing which side is correct.

If a reliable plan cannot be produced, return only:

{
  "planning_status": "failed",
  "planning_error": "short reason"
}
