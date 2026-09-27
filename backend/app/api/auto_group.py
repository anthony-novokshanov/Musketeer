"""Auto group: a plain-English project description -> a small team whose recent work covers what it needs.
Muse names the skills; the temporal expertise graph picks people (greedy coverage, no extra AI calls)."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.ai.muse import muse_json
from app.db import fetch_all
from app.expertise import vectors
from app.matching import candidates
from app.schemas import RequiredSkillsOut

router = APIRouter()

MIN_GAIN = 0.02   # a person must add at least this much weighted coverage to be picked for coverage


class AutoGroupIn(BaseModel):
    description: str = Field(min_length=10, max_length=2000)
    size: int = Field(default=4, ge=2, le=8)


def pick_team(cover, people: list[str], required: list[dict], size: int) -> list[str]:
    """Greedy weighted coverage: each pick adds the most uncovered skill weight; ties go to overall relevance.
    cover(person, skill_id) -> 0..1. Once nobody adds coverage, the rest are filled by overall relevance."""
    total = sum(r["weight"] for r in required) or 1.0
    relevance = {p: sum(r["weight"] * cover(p, r["skill_id"]) for r in required) / total for p in people}
    best = {r["skill_id"]: 0.0 for r in required}
    team: list[str] = []
    while len(team) < size:
        gains = {p: sum(r["weight"] * max(0.0, cover(p, r["skill_id"]) - best[r["skill_id"]]) for r in required)
                 for p in people if p not in team}
        if not gains:
            break
        p = max(gains, key=lambda q: (gains[q], relevance[q], q))
        if gains[p] < MIN_GAIN:
            break
        team.append(p)
        for r in required:
            best[r["skill_id"]] = max(best[r["skill_id"]], cover(p, r["skill_id"]))
    rest = sorted((p for p in people if p not in team and relevance[p] > 0), key=lambda q: (-relevance[q], q))
    return team + rest[:size - len(team)]


@router.post("/auto-group")
async def auto_group(body: AutoGroupIn):
    try:
        out = await muse_json("required_skills", {"text": body.description, "skills": await candidates.skill_names()},
                              RequiredSkillsOut)
    except Exception as e:
        raise HTTPException(502, f"could not read the project description: {e}")
    required = await candidates.resolve_required(out.required_skills)
    space = await vectors.load()
    people = {r["id"]: r for r in await fetch_all("""
        SELECT p.id, p.name, p.title, t.id AS team_id, t.name AS team_name
        FROM people p JOIN teams t ON t.id = p.team_id WHERE p.available""")}
    team = pick_team(space.cover, [p for p in space.people if p in people], required, body.size)

    name = lambda sid: space.names.get(sid, sid)
    skills = []
    for r in sorted(required, key=lambda r: -r["weight"]):
        levels = {p: space.cover(p, r["skill_id"]) for p in team}
        top = max(levels, key=levels.get, default=None)
        skills.append({"skill_id": r["skill_id"], "name": name(r["skill_id"]), "weight": r["weight"],
                       "coverage": round(levels[top], 2) if top else 0.0,
                       "covered_by": top if top and levels[top] > 0 else None})
    members = []
    for p in team:
        strengths = sorted(({"name": name(r["skill_id"]), "level": round(space.cover(p, r["skill_id"]), 2)}
                            for r in required), key=lambda s: -s["level"])
        members.append({"person_id": p, **{k: v for k, v in people[p].items() if k != "id"},
                        "skills": [s for s in strengths if s["level"] > 0]})
    total = sum(s["weight"] for s in skills) or 1.0
    return {"skills": skills, "members": members,
            "coverage": round(sum(s["weight"] * s["coverage"] for s in skills) / total, 2)}
