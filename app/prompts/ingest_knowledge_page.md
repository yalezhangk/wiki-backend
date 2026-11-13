Resolve and generate exactly one planned Entity or Concept page for a large-document Ingest.

Use only this page plan, its structured source evidence, and its relevant retrieved Wiki sections. Do not process any other planned page and do not invent existing paths.

Page plan:
${page_plan_json}

Structured source evidence for this page:
${evidence_json}

Relevant retrieved Wiki sections:
${wiki_context}

Return only one valid JSON object using exactly one action.

For a genuinely new page:

{
  "generation_status": "succeeded",
  "generation_error": null,
  "target_id": "${target_id}",
  "action": "create",
  "page_type": "${page_type}",
  "title": "planned title",
  "path": "exact planned path",
  "content": "complete source-grounded Markdown page",
  "index_summary": "one-line source-grounded summary"
}

For an update to a retrieved existing page:

{
  "generation_status": "succeeded",
  "generation_error": null,
  "target_id": "${target_id}",
  "action": "patch",
  "page_type": "${page_type}",
  "path": "exact retrieved Entity or Concept path",
  "base_hash": "exact snapshot hash supplied with that path",
  "operation": "append_section|replace_section",
  "heading": "one level-2 heading without #",
  "content": "source-grounded section body only"
}

When the evidence adds no reliable reusable knowledge or the retrieved page already contains it:

{
  "generation_status": "succeeded",
  "generation_error": null,
  "target_id": "${target_id}",
  "action": "skip",
  "page_type": "${page_type}",
  "reason": "short reason"
}

Rules:
- create must use the exact planned title and path and may not target a retrieved existing path.
- patch may target only a path and hash explicitly supplied in Relevant retrieved Wiki sections.
- Never patch overview.md, a Source page, Frontmatter, Related, or Sources.
- replace_section may replace only a supplied existing heading. append_section must use a new heading.
- Preserve exact numbers, units, models, conditions, exclusions, and negations from the supplied evidence.
- Prefer skip over an unsupported or redundant change.

If this target cannot be processed reliably, return only:

{
  "generation_status": "failed",
  "generation_error": "short reason",
  "target_id": "${target_id}"
}
