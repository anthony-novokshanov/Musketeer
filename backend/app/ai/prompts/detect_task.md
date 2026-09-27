You decide whether an incoming message gives its recipient a new piece of work.

Recipient: {{recipient_name}}, {{recipient_title}}, {{recipient_team}}

Message title: {{title}}
Message text:
{{text}}

A new task is a request, assignment, or problem the recipient is now expected to handle
(e.g. "Can you run our booth at the hackathon?", a review request, a bug report assigned to them).
Not tasks: FYIs, thanks, social chat, newsletters, status updates, replies that close a thread.

Known skills in the company:
{{skills}}

Return JSON:
- is_new_task: true if this gives the recipient new work.
- confidence: 0-1, how sure you are.
- summary: the task in <= 25 words, phrased as the work itself (e.g. "Run the company booth at the MLH hackathon next month").
- task_type: one of event, project, bug, review, request, other.
- team_level: true if the work clearly belongs to the recipient's whole team (a team program, a team
  launch, work the team will staff together), false if it is this person's own task.
- required_skills: 1-4 skills or problem areas someone would need to have worked on to help,
  each {label, weight}. label: a lowercase 1-4 word problem-level name. Reuse a known skill name only if it is the same
  kind of work; otherwise write your own label. weight: 0-1, how central it is to the task. Use [] when is_new_task is false.
