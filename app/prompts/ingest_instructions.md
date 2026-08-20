# Ingest Wiki Instructions

Only perform the Ingest workflow described below. Do not perform query,
health, lint, graph, publishing, or any other workflow.

## Ingest Scope and Wiki Layout

The source document is immutable input. The Wiki is composed of:

- `wiki/index.md`: catalog of knowledge pages.
- `wiki/overview.md`: high-level synthesis across sources.
- `wiki/sources/`: one source page per ingested document.
- `wiki/entities/`: focused pages for people, companies, projects, and products.
- `wiki/concepts/`: focused pages for ideas, frameworks, methods, and theories.
- `wiki/syntheses/`: saved answers; do not modify these during Ingest.

Use `[[PageName]]` Wikilinks only when the linked page exists in the supplied
Wiki context or is created by this Ingest result. Do not invent facts, source
metadata, contradictions, or pages unsupported by the source and supplied Wiki
context.

## Ingest Workflow

Process the source in this order:

1. Read the supplied source document and determine whether it contains usable,
   reliable information.
2. Use the supplied Wiki context to identify related pages and possible
   contradictions.
3. Create exactly one source page at `wiki/sources/<slug>.md`.
4. Provide one entry for the `## Sources` section of `wiki/index.md`.
5. Return a complete replacement for `wiki/overview.md` only when the source
   materially changes the high-level synthesis; otherwise return `null`.
6. Create or update focused entity and concept pages only for key information
   grounded in the source. Preserve existing facts unless the source provides a
   concrete correction.
7. Report contradictions only when both the source and existing Wiki context
   support them.
8. Provide a newest-first ingest log entry in the requested format.

If the source is unreadable, corrupted, insufficient, or cannot be processed
reliably, report failure rather than inventing a successful result.

## Source Page Requirements

The source page must contain complete YAML Frontmatter with:

```yaml
---
title: "Source Title"
type: source
tags: []
date: YYYY-MM-DD
---
```

Its title must match the result title. Include a concise summary, key claims,
source-grounded quotes when useful, connections to relevant entity/concept
pages, and supported contradictions when present. Adapt section names for
domain-specific documents such as diaries or meeting notes, while preserving
the required Frontmatter and source grounding.

The backend assigns the final source-origin field: manual sources receive
`source_file`, and scheduled sources receive `source_url`. Do not emit either
field yourself.

## Naming and Index Rules

- Source slugs use kebab-case and match the source filename where practical.
- Entity and concept paths use focused, readable TitleCase names.
- The index entry must link to `sources/<slug>.md` and provide a one-line
  source-grounded summary.
- The log entry format is `## [YYYY-MM-DD] ingest | <title>` and describes the
  principal claims added by this ingestion.
