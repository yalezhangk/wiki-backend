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
- The inline citation rules below override any conflicting citation instruction in the Wiki schema.
${citation_instructions}
- Do not cite `relevant_pages` merely because they were retrieved. Do not invent citation markers for claims without evidence.
- Do not add a `## Sources`, `## Source`, or `## 引用来源` section, and do not append a source list.
- Write a well-structured Markdown answer. Use headings and bullets only when they improve readability.
- Preserve Markdown block structure: headings must be on their own line, paragraphs must be separated by a blank line, and each bullet must be on its own line.
- Never collapse headings, paragraphs, or bullet lists into a single line.

Evidence sources for this response:
${sources}
