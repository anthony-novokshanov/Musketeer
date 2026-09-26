You are writing realistic work history for one employee of a demo company: {{company}}.
It will be used to figure out who does similar work, so be concrete and specific.

Employee: {{name}}, {{title}}
Team: {{team}} ({{org}})
Manager: {{manager}}
Teammates: {{teammates}}

What this person works on:
{{persona}}

Covering the last {{history_days}} days (days_ago between 1 and {{history_days}}), write:
{{counts}}

Code rules (engineers only):
- Every file path is "<directory>/<filename>" where <directory> is exactly one of:
  {{directories}}
  Put files directly in that directory (no extra subfolders). Use real-looking filenames and extensions.
- All files in one PR belong to directories from the same repo prefix list above.
- Commit messages mostly follow conventional commits (feat:, fix:, perf:, refactor:, test:, docs:, chore:).
- PR titles are short; bodies <= 400 characters explaining what changed and why.

Writing rules:
- Slack messages <= 280 characters, written the way people actually post at work.
- Emails (if any) are ones this person sent: subject plus a body <= 600 characters.
- Mention real specifics: systems, events, metrics, vendors, dates. Vary the wording.
- Stay in this person's lane. Do not invent work outside what is described above.

Return JSON: {prs, slack_messages, emails} (use empty lists where a kind is not requested).
Each PR: {title, body, files, commits, days_ago}; each Slack message: {text, days_ago};
each email: {subject, body, days_ago}.
