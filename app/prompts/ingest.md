You are maintaining an LLM Wiki. Process this source document and integrate its knowledge into the Wiki.

Wiki schema and conventions:
${schema}

Current Wiki state:
${wiki_context}

New source to ingest:

```text
${source_label}
```

=== SOURCE START ===
${source_content}
=== SOURCE END ===

Today's date: ${today}

Return only a valid JSON object with these fields. Do not include Markdown fences or prose outside the JSON object:

{
  "title": "Human-readable title for this source",
  "slug": "kebab-case-slug-for-filename",
  "source_page": "full Markdown content for wiki/sources/<slug>.md with useful inline [[Wikilinks]]",
  "index_entry": "- [Title](sources/slug.md) - one-line summary",
  "overview_update": null,
  "entity_pages": [
    {"path": "entities/EntityName.md", "content": "full Markdown content"}
  ],
  "concept_pages": [
    {"path": "concepts/ConceptName.md", "content": "full Markdown content"}
  ],
  "contradictions": ["describe contradictions with existing Wiki content, or use an empty list"],
  "log_entry": "## [${today}] ingest | <title>\n\nAdded source. Key claims: ..."
}

Important:
- Always set `"overview_update"` to `null`. Do not rewrite `wiki/overview.md` in this response.
- Keep generated entity and concept pages focused.
- Prefer a complete source page, index entry, contradiction list, and log entry.
- Return complete JSON that can be parsed by `json.loads`.
