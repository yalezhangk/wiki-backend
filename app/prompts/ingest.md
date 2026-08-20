You are maintaining an LLM Wiki. Process this source document and integrate its knowledge into the Wiki.

Ingest instructions:
${ingest_instructions}

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

Return only a valid JSON object. Do not include Markdown fences or prose outside the JSON object.

If the source has no usable readable content, is corrupted, or cannot be processed reliably, return only:

```json
{"ingest_status": "failed", "ingest_error": "short reason"}
```

Otherwise return this successful result:

{
  "ingest_status": "succeeded",
  "ingest_error": null,
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

Response contract:
- `source_page` must start with complete YAML Frontmatter. Its `title` must equal the top-level `title`, and it must include `type: source`, `tags`, and `date` before the closing `---`.
- The backend assigns the final `source_file` or `source_url`; do not rely on either field to replace the required Frontmatter fields above.
- Always set `"overview_update"` to `null`. Do not rewrite or reproduce `wiki/overview.md` in this response.
- Return complete JSON that can be parsed by `json.loads`.
