Two colleagues who have never talked are being connected in a Slack group DM.
Write the opening message that gets them talking.

Requester (has the new task):
{{requester}}

Helper (has done similar work):
{{helper}}

The task: {{task_summary}}
Why they were matched: {{reason}}

Return JSON:
- message: <= 90 words, warm and specific. Introduce them by first name, say what the
  requester is working on and what the helper has done that is relevant. No hype, no emojis.
- questions: exactly 3 short questions the requester could ask the helper to start.
