"""Temporal candidate generation (spec §9.1), shared by both matcher modes.

For a person: top CANDIDATE_POOL by temporal_sim, plus anyone sharing a GitHub directory (capped at 20).
For a task, search, or group: required skills -> top CANDIDATE_POOL by task_relevance.
"""
from app.config import settings
from app.db import fetch_all
from app.expertise.canonicalize import canonicalize, normalize_label
from app.expertise.vectors import SkillSpace
from app.schemas import RequiredSkill

MAX_PERSON_CANDIDATES = 20


def for_person(space: SkillSpace, person_id: str, dirs: dict[str, set[str]]) -> list[str]:
    ids = [p for p, _ in space.similar_people(person_id)[:settings.CANDIDATE_POOL]]
    mine = dirs.get(person_id, set())
    if mine:
        ids += [p for p, d in sorted(dirs.items()) if p != person_id and p not in ids and d & mine]
    return ids[:MAX_PERSON_CANDIDATES]


def for_skills(space: SkillSpace, required: list[dict], exclude: set[str] = frozenset()) -> list[tuple[str, float]]:
    """[(person_id, task_relevance)] best first."""
    return space.rank_for_skills(required, exclude)[:settings.CANDIDATE_POOL]


async def skill_names() -> str:
    """The canonical vocabulary as a prompt list (P1)."""
    rows = await fetch_all("SELECT name FROM skills ORDER BY name")
    return ", ".join(r["name"] for r in rows) or "(none yet)"


async def resolve_required(items: list[RequiredSkill]) -> list[dict]:
    """P1 labels -> [{"skill_id", "weight"}] (tasks.required_skills); duplicates keep the max weight."""
    items = [i for i in items[:4] if i.label.strip()]
    if not items:
        return []
    mapping = await canonicalize([i.label for i in items])
    weights: dict[str, float] = {}
    for i in items:
        sid = mapping[normalize_label(i.label)]
        weights[sid] = max(weights.get(sid, 0.0), max(0.0, min(1.0, i.weight)))
    return [{"skill_id": s, "weight": w} for s, w in weights.items() if w > 0]
