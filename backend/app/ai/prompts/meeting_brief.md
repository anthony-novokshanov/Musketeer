Two colleagues who were just matched have booked a meeting. Draft the context section of their
shared meeting page so they walk in prepared. They will edit it, so be useful and brief.

Requester (has the new work):
{{requester}}

Helper (has done similar work):
{{helper}}

The work: {{task_summary}}
Why they were matched: {{reason}}

Return JSON:
- why_matched: one or two sentences on the concrete overlap between them.
- task: one or two sentences restating what the requester needs to get done.
- background: up to 4 short bullets of relevant background drawn only from the profiles above.
- questions: exactly 3 questions the requester could ask the helper.
- work_on: up to 3 concrete things they could work on together during the meeting.
Do not invent facts that are not in the profiles.
