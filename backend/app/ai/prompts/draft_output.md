Two colleagues are working from a shared meeting page. Turn their notes into the output they asked for.

What they want: {{kind}}

Their meeting page (plain text):
{{notes}}

Rules:
- Use only what is on the page. Do not invent decisions, owners, deadlines, or facts.
- If the page does not say who owns something or when it is due, leave that out.
- Keep anything unresolved out of the draft and list it in open_questions instead.
- The page already has its title and sections. Return only the new content: no headings, no title,
  and do not repeat or restate the page's structure.
- For a checklist, markdown is only "- [ ] item" lines. Otherwise use plain paragraphs and bullets.
- Write for the two of them, in plain language.

Return JSON:
- title: a short title for the output (<= 8 words).
- markdown: the content itself, with no headings (<= 250 words).
- open_questions: questions or decisions the page leaves unresolved (may be empty).
