from fastapi import APIRouter, HTTPException

from app.api.queries import LATEST_CONN, edge_state
from app.config import settings
from app.db import fetch_all, fetch_one
from app.pipeline.profiles import github_aggregates
from app.schemas import PersonOut

router = APIRouter()


@router.get("/people/{person_id}", response_model=PersonOut)
async def person(person_id: str):
    p = await fetch_one("""
        SELECT p.id, p.name, p.title, p.summary, p.focus_areas, p.is_engineer,
               json_build_object('id', t.id, 'name', t.name, 'org_id', t.org_id) AS team
        FROM people p JOIN teams t ON t.id = p.team_id WHERE p.id = %s""", (person_id,))
    if not p:
        raise HTTPException(404, f"person {person_id} not found")

    github = None
    if p["is_engineer"]:
        agg = (await github_aggregates()).get(person_id)
        if agg:
            github = {k: agg[k] for k in ("top_directories", "languages", "commit_mix")}

    open_tasks = await fetch_all("""
        SELECT id, summary, created_at, status FROM tasks
        WHERE person_id = %s AND status IN ('open', 'notified') ORDER BY created_at DESC""", (person_id,))

    matches = await fetch_all(f"""
        WITH {LATEST_CONN}
        SELECT o.id AS person_id, o.name, t.name AS team_name, s.score, s.reason, s.shared_dirs,
               ARRAY(SELECT name FROM skills k JOIN unnest(s.shared_skills) WITH ORDINALITY u(id, i) ON k.id = u.id ORDER BY u.i) AS shared_skills,
               {edge_state('l.status')} AS state
        FROM similarities s
        JOIN people o ON o.id = CASE WHEN s.person_a = %(p)s THEN s.person_b ELSE s.person_a END
        JOIN teams t ON t.id = o.team_id
        LEFT JOIN latest l ON l.a = s.person_a AND l.b = s.person_b
        WHERE %(p)s IN (s.person_a, s.person_b)
        ORDER BY s.score DESC LIMIT %(k)s""", {"p": person_id, "k": settings.TOPK_STORE})

    connections = await fetch_all("""
        SELECT c.id, o.id AS other_id, o.name AS other_name, c.status, c.origin, c.created_at,
               CASE WHEN c.requester_id = %(p)s THEN c.helpful_requester ELSE c.helpful_helper END AS helpful
        FROM connections c
        JOIN people o ON o.id = CASE WHEN c.requester_id = %(p)s THEN c.helper_id ELSE c.requester_id END
        WHERE %(p)s IN (c.requester_id, c.helper_id)
        ORDER BY c.created_at DESC""", {"p": person_id})

    # Current skills as chips; never trends or history (spec §12 skill exposure rule).
    skills = await fetch_all("""
        SELECT st.skill_id, s.name, CASE WHEN st.level >= 0.7 THEN 'strong' WHEN st.level >= 0.4 THEN 'solid'
                                         ELSE 'some' END AS level_label
        FROM person_skill_state st JOIN skills s ON s.id = st.skill_id
        WHERE st.person_id = %s ORDER BY st.level DESC, s.name LIMIT 8""", (person_id,))

    return {**p, "skills": skills, "github": github, "open_tasks": open_tasks, "matches": matches, "connections": connections}
