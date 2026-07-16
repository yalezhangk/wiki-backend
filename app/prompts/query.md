You are querying an LLM Wiki to answer a question. Use the Wiki pages below to synthesize a thorough answer.

Wiki schema:
${schema}

Conversation history:
${conversation_history}

Relevant wiki pages:
${pages_context}

Current user question:
${question}

Requirements:
- Use the conversation history only to resolve context, references, and ellipsis.
- The final answer must still be grounded in the wiki pages above.
- Start with the answer itself. Do not repeat, quote, or paraphrase the current user question.
- Do not use the current user question as the answer title or as a heading.
- Cite sources using `[[PageName]]` wikilink syntax.
- Write a well-structured Markdown answer. Use headings and bullets only when they improve readability.
- Preserve Markdown block structure: headings must be on their own line, paragraphs must be separated by a blank line, and each bullet must be on its own line.
- Never collapse headings, paragraphs, or bullet lists into a single line.
- At the end, add a `## Sources` section listing the pages used.
