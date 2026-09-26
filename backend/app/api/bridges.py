from fastapi import APIRouter, HTTPException

from app.db import fetch_all, fetch_one
from app.schemas import BridgeOut

router = APIRouter()

_TEAM = """SELECT t.id, t.name,
                  CASE WHEN l.id IS NULL THEN NULL ELSE json_build_object('id', l.id, 'name', l.name) END AS lead
           FROM teams t LEFT JOIN people l ON l.id = t.lead_id WHERE t.id = %s"""


@router.get("/bridges/{team_a}/{team_b}", response_model=BridgeOut)
async def bridge(team_a: str, team_b: str):
    lo, hi = sorted((team_a, team_b))
    overlap = await fetch_one("""SELECT score, summary, shared_topics, lead_brief FROM team_overlaps
                                 WHERE team_a = %s AND team_b = %s""", (lo, hi))
    if not overlap:
        raise HTTPException(404, f"no overlap between {team_a} and {team_b}")

    top_pairs = await fetch_all("""
        SELECT s.person_a AS a, s.person_b AS b, pa.name AS a_name, pb.name AS b_name, s.score, s.reason
        FROM similarities s JOIN people pa ON pa.id = s.person_a JOIN people pb ON pb.id = s.person_b
        WHERE (pa.team_id, pb.team_id) IN ((%(x)s, %(y)s), (%(y)s, %(x)s))
        ORDER BY s.score DESC LIMIT 5""", {"x": lo, "y": hi})

    count = await fetch_one("""
        SELECT count(*) AS n FROM connections c
        JOIN people r ON r.id = c.requester_id JOIN people h ON h.id = c.helper_id
        WHERE c.status IN ('accepted', 'active') AND (r.team_id, h.team_id) IN ((%(x)s, %(y)s), (%(y)s, %(x)s))""",
        {"x": lo, "y": hi})

    return {"team_a": await fetch_one(_TEAM, (team_a,)), "team_b": await fetch_one(_TEAM, (team_b,)),
            **overlap, "top_pairs": top_pairs, "connections_count": count["n"]}
