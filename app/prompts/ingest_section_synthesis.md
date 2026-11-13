Compact the adjacent structured source records below into one traceable section synthesis.

Do not use outside knowledge, generate Wiki pages, or invent evidence. Deduplicate repeated facts and aliases, but preserve model numbers, numbers, units, conditions, exclusions, negations, warnings, and explicit conflicts. Low-information chunks must remain covered even when they produce no key claim.

Section metadata:
- section_id: ${section_id}
- today's date: ${today}
- input representation: ${input_type}

=== STRUCTURED SECTION INPUT START ===
${section_items_json}
=== STRUCTURED SECTION INPUT END ===

Return only one valid JSON object with this shape:

{
  "synthesis_status": "succeeded",
  "synthesis_error": null,
  "section_id": "${section_id}",
  "covered_chunk_ids": ["every represented chunk ID exactly once"],
  "summary": "compact source-grounded section summary",
  "key_claims": [
    {
      "statement": "one deduplicated claim retaining exact qualifiers",
      "supporting_evidence_ids": ["existing evidence IDs only"]
    }
  ],
  "entities": ["deduplicated source-named entities"],
  "concepts": ["deduplicated source-grounded concepts"],
  "internal_conflicts": ["explicit conflicts only"]
}

If the records cannot be synthesized reliably, return only:

{
  "synthesis_status": "failed",
  "synthesis_error": "short reason",
  "section_id": "${section_id}"
}
