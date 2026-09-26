Write a short work profile of this employee from their recent activity.

Person: {{name}}, {{title}}, {{team}}

GitHub aggregates (last 60 days; "none" for non-engineers):
{{github}}

Recent work, newest first:
{{events}}

Return JSON:
- summary: <= 60 words describing the concrete work they do and problems they have solved.
  Name specific systems, projects, and events. No praise, no filler.
- focus_areas: up to 6 lowercase topics, 1-3 words each (e.g. "consumer lag", "hackathon sponsorship").
