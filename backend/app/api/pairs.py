from fastapi import APIRouter, HTTPException

from app.db import fetch_all, fetch_one
from app.schemas import PairOut

router = APIRouter()

_SIDE = """SELECT p.id, p.name, t.name AS team_name FROM people p JOIN teams t ON t.id = p.team_id WHERE p.id = %s"""


@router.get("/pairs/{a}/{b}", response_model=PairOut)
async def pair(a: str, b: str):
    side_a, side_b = await fetch_one(_SIDE, (a,)), await fetch_one(_SIDE, (b,))
    if not side_a or not side_b:
        raise HTTPException(404, "person not found")
    lo, hi = sorted((a, b))
    sim = await fetch_one("""SELECT semantic_score, temporal_score, dir_overlap, score, reason, shared_dirs,
                                    ARRAY(SELECT name FROM skills k JOIN unnest(s.shared_skills) WITH ORDINALITY u(id, i) ON k.id = u.id ORDER BY u.i) AS shared_skills
                             FROM similarities s WHERE person_a = %s AND person_b = %s""", (lo, hi)) or {}

    connections = await fetch_all("""
        SELECT c.id, c.status, c.origin, t.summary AS task_summary, c.created_at, c.accepted_at,
               c.message_count, c.helpful_requester, c.helpful_helper, c.request_note, c.request_links,
               COALESCE((SELECT json_agg(json_build_object('time', e.time, 'event', e.event, 'person_id', e.person_id)
                                ORDER BY e.time)
                         FROM connection_events e WHERE e.connection_id = c.id AND e.event <> 'message'), '[]') AS timeline,
               COALESCE((SELECT json_agg(json_build_object('id', m.id, 'start_at', m.start_at, 'canvas_url', m.canvas_url,
                                  'event_link', m.event_link, 'meet_link', m.meet_link, 'deliverable', m.deliverable,
                                  'is_follow_up', m.parent_id IS NOT NULL) ORDER BY m.start_at)
                         FROM meetings m WHERE m.connection_id = c.id), '[]') AS meetings
        FROM connections c LEFT JOIN tasks t ON t.id = c.task_id
        WHERE LEAST(c.requester_id, c.helper_id) = %s AND GREATEST(c.requester_id, c.helper_id) = %s
        ORDER BY c.created_at DESC""", (lo, hi))

    return {"a": side_a, "b": side_b, "semantic_score": sim.get("semantic_score"),
            "temporal_score": sim.get("temporal_score"), "shared_skills": sim.get("shared_skills", []),
            "dir_overlap": sim.get("dir_overlap"), "score": sim.get("score"), "reason": sim.get("reason"),
            "shared_dirs": sim.get("shared_dirs", []), "connections": connections}
