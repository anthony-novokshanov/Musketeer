You extract evidence of expertise from one piece of work a person did.

Person: {{person_name}}, {{person_title}}, {{person_team}}

Work item ({{kind}}):
Title: {{title}}
Text:
{{text}}
{{details}}

Return 0-4 items. Each item is one skill used or problem handled in this work.

- label: what the work was about, as a short lowercase phrase (1-4 words) naming the problem solved
  or skill used, not the artifact. Good: "kafka consumer lag", "hackathon booth logistics",
  "gateway rate limiting". Bad: "PR 412", "slack message", "email", "code change".
- evidence_kind:
  - built: authored a PR adding a capability.
  - solved: a fix or performance PR, or a message reporting a resolved problem.
  - reviewed: a code review.
  - organized: ran or coordinated an event, program, or process.
  - discussed: gave substantive help or advice in a public thread.
- confidence: 0-1, how clearly this work shows the person used that skill.
- snippet: a paraphrase of what they did, <= 120 characters, e.g. "fixed consumer lag via batching".
  No names of people, no quoted text.

Chit-chat, scheduling, thanks, and status pings produce zero items. Be conservative: return an empty
list rather than guessing.
