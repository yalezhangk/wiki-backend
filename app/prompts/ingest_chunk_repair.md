Repair the invalid response below into one valid JSON object matching the chunk extraction contract.

Only repair JSON syntax and structure already present in the invalid response. Do not add facts, evidence, entities, concepts, source lines, or content that is absent from it. Keep at most 20 highest-value evidence items and at most 20 entries in each other list; remove repetitive or lower-value items when necessary. Keep the summary within 500 characters, every statement within 300 characters, and every evidence_text within 200 characters. Return no Markdown fences, prose, apologies, or analysis outside the JSON object.

Required successful fields:
- extraction_status="succeeded"
- extraction_error=null
- chunk_id
- section_title
- summary
- evidence
- entities
- concepts
- internal_conflicts

Required failed fields:
- extraction_status="failed"
- extraction_error
- chunk_id

Expected chunk_id: ${chunk_id}

=== INVALID RESPONSE START ===
${invalid_response}
=== INVALID RESPONSE END ===
