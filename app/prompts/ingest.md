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
    {"path": "entities/EntityName.md", "content": "full Markdown content for a new page only"}
  ],
  "concept_pages": [
    {"path": "concepts/ConceptName.md", "content": "full Markdown content for a new page only"}
  ],
  "entity_index_entries": [
    {"path": "entities/EntityName.md", "entry": "- [Entity Name](entities/EntityName.md) - one-line source-grounded summary"}
  ],
  "concept_index_entries": [
    {"path": "concepts/ConceptName.md", "entry": "- [Concept Name](concepts/ConceptName.md) - one-line source-grounded summary"}
  ],
  "entity_patches": [
    {"path": "entities/Existing.md", "base_hash": "snapshot hash shown in context", "operation": "append_section", "heading": "New evidence", "content": "source-grounded Markdown"}
  ],
  "concept_patches": [
    {"path": "concepts/Existing.md", "base_hash": "snapshot hash shown in context", "operation": "replace_section", "heading": "Existing heading", "content": "replacement body only"}
  ],
  "contradictions": ["describe contradictions with existing Wiki content, or use an empty list"],
  "log_entry": "## [${today}] ingest | <title>\n\nAdded source. Key claims: ..."
}

Response contract:
- `source_page` must start with complete YAML Frontmatter. Its `title` must equal the top-level `title`, and it must include `type: source`, `tags`, and `date` before the closing `---`.
- The backend assigns the final `source_file` or `source_url`; do not rely on either field to replace the required Frontmatter fields above.
- Always set `"overview_update"` to `null`. Do not rewrite or reproduce `wiki/overview.md` in this response.
- `entity_pages` and `concept_pages` may create new paths only. Never return an existing page in either list.
- Each new Entity or Concept must have exactly one matching entry in `entity_index_entries` or `concept_index_entries`; the entry must link to its exact path. Do not return an index entry for an existing page or a patch.
- An existing Entity or Concept may be updated only when its path and snapshot hash were supplied in the Wiki context. Use a patch: `append_section` adds a new unique level-2 heading; `replace_section` replaces only the body below an existing level-2 heading. Never modify Frontmatter, `sources`, `Related`, or any unselected section.
- Return complete JSON that can be parsed by `json.loads`.
