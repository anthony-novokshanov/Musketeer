You map one new raw skill label onto an existing skill vocabulary.

New label: {{label}}

Existing skills (name: description):
{{skills}}

If the label means the same problem or skill as an existing one (different wording counts as the
same), return that skill's name exactly as listed, with is_new = false.

Only if nothing fits, create a new skill: is_new = true, skill_name = a lowercase 1-4 word
problem-level name, kind = problem | domain | tool | practice, description = one line.
Prefer mapping to an existing skill over creating a new one.
