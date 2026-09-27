You build a small, canonical vocabulary of skills from raw labels extracted from people's work.

Raw labels (one per line, with how many times each appeared):
{{labels}}

Group the raw labels into canonical skills. Produce {{target_min}}-{{target_max}} skills in total;
fewer than {{target_min}} means you merged distinct problems together.

- A skill is a kind of work one colleague could help another with. Merge labels when someone who
  did one could directly help with the other, whatever the wording or context: "consumer lag",
  "messages piling up in the queue", and "kafka backlog" are one skill; booth logistics at a
  hackathon and at a career fair are one skill.
- Keep different kinds of work separate even when they share a tool, team, or artifact: building
  dashboards and charts is a different skill from making a UI accessible; kafka consumer lag is
  different from message ordering. Never name a skill after a tool or a whole area alone.
- Prefer problem-level names over artifacts or file names.
- name: lowercase, 1-4 words, unique.
- kind: problem (a recurring problem people solve), domain (an area of work), tool (a specific
  technology), or practice (a way of working).
- description: one line.
- raw_labels: every raw label that belongs to this skill, copied exactly as given. Every raw label
  above must appear in exactly one skill.
- related: names of up to 3 other skills in your list that are closely related (someone strong in
  one could partly help with the other). Use [] if none.
