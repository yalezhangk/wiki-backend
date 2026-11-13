Generate the single Source page for a large-document Ingest.

Use only the document plan and structured source evidence below. Do not invent facts, recover omitted details from general knowledge, plan other pages, or modify existing Wiki pages.

Document plan:
${document_plan_json}

Allowed Wiki links are limited to these retrieved or planned pages:
${allowed_wiki_links}

Structured source evidence:
${evidence_json}

Today's date: ${today}

Return only one valid JSON object:

{
  "generation_status": "succeeded",
  "generation_error": null,
  "target_id": "source",
  "title": "${title}",
  "slug": "${slug}",
  "content": "complete Markdown for sources/${slug}.md"
}

Content requirements:
- Start with YAML Frontmatter containing title exactly "${title}", type: source, tags, and date: ${today}.
- Do not emit source_file or source_url; the backend assigns the canonical origin.
- Cover the document's substantive sections rather than returning a generic summary.
- Preserve source-grounded model numbers, ratings, dimensions, units, environmental conditions, installation requirements, warnings, limitations, exclusions, and negations.
- Use only evidence supplied above. Quotes must remain faithful to evidence_text.
- Use Wiki links only from the allowed list. Copy the exact path target shown there; an optional display alias may follow `|`.

If the page cannot be generated reliably, return only:

{
  "generation_status": "failed",
  "generation_error": "short reason",
  "target_id": "source"
}
