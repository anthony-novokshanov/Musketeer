Two colleagues ({{people}}) are wrapping up a meeting. From their meeting page, propose next steps.

Their meeting page (plain text):
{{notes}}

Rules:
- Only steps the page supports. At most 5.
- owner: one of {{people}}, only if the page makes it clear who is doing it; otherwise null.
- due: a date as YYYY-MM-DD, only if the page states one; otherwise null. Today is {{today}}.

Return JSON: steps, each {step, owner, due}. step is <= 15 words, starting with a verb.
