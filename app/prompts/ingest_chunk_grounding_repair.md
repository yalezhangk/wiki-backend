Repair the evidence grounding in one already parsed chunk extraction.

The prior result failed strict backend schema or grounding validation with category `${validation_category}`. Use only the numbered primary source below. Do not add outside facts or infer missing values.

Rules:
- Return the complete successful chunk extraction JSON object, not a patch.
- Keep at most 20 highest-value evidence items and at most 20 entries in each other list; keep the summary within 500 characters, every statement within 300 characters, and every evidence_text within 200 characters.
- Keep chunk_id exactly `${chunk_id}`.
- Every evidence_text must be one short contiguous verbatim excerpt from the declared source line or inclusive line range.
- source_lines must be one line number or one increasing inclusive range such as `120-123`; never return comma-separated or non-contiguous line numbers.
- Read source_lines from the backend-added `L<number>:` prefixes. The prefixes are metadata and must not appear in evidence_text.
- Do not join separate excerpts with ellipses.
- Correct an evidence item only when the source supports it exactly; otherwise delete that evidence item.
- Renumber the remaining evidence IDs as `${chunk_id}:e001`, `${chunk_id}:e002`, and so on without duplicates.
- Preserve exact numbers, units, model names, conditions, exclusions, and negations.
- Do not include Markdown fences, explanations, apologies, or analysis.

Allowed absolute source lines: ${source_line_range}

=== NUMBERED PRIMARY SOURCE START ===
${primary_text}
=== NUMBERED PRIMARY SOURCE END ===

=== INVALID EXTRACTION START ===
${invalid_response}
=== INVALID EXTRACTION END ===
