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
    sim = await fetch_one("""SELECT semantic_score, dir_overlap, score, reason, shared_dirs
                             FROM similarities WHERE person_a = %s AND person_b = %s""", (lo, hi)) or {}

    connections = await fetch_all("""
        SELECT c.id, c.status, c.origin, t.summary AS task_summary, c.created_at, c.accepted_at,
               c.message_count, c.helpful_requester, c.helpful_helper,
               COALESCE((SELECT json_agg(json_build_object('time', e.time, 'event', e.event, 'person_id', e.person_id)
                                ORDER BY e.time)
                         FROM connection_events e WHERE e.connection_id = c.id AND e.event <> 'message'), '[]') AS timeline
        FROM connections c LEFT JOIN tasks t ON t.id = c.task_id
        WHERE LEAST(c.requester_id, c.helper_id) = %s AND GREATEST(c.requester_id, c.helper_id) = %s
        ORDER BY c.created_at DESC""", (lo, hi))

    return {"a": side_a, "b": side_b, "semantic_score": sim.get("semantic_score"),
            "dir_overlap": sim.get("dir_overlap"), "score": sim.get("score"), "reason": sim.get("reason"),
            "shared_dirs": sim.get("shared_dirs", []), "connections": connections}
