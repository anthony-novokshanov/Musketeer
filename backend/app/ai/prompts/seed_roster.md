You are creating the employee roster for a realistic demo company: {{company}}.

Fill in every slot below with a person. Each slot gives the team, org, whether they are the
team lead, whether they are an engineer, and sometimes a fixed name that you must use exactly.

{{slots}}

Rules:
- Every name is unique, realistic, and the roster is diverse. Use "First Last" format.
- Titles fit the team and seniority. Leads get lead/manager titles (e.g. "Engineering Manager,
  Payments Infrastructure", "Lead, University Recruiting"). Engineers get engineering titles.
  Non-engineers get titles that fit their org (recruiters, product managers, designers).
- Return every slot_id exactly once.

Return JSON: people, a list of {slot_id, name, title}.
