from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.api.queries import LATEST_CONN, edge_state, needs_connection
from app.config import settings
from app.db import fetch_all, fetch_one
from app.schemas import GraphOut

router = APIRouter()


@router.get("/graph", response_model=GraphOut)
async def graph(viewer_id: str | None = None):
    viewer_id = viewer_id or settings.DEFAULT_VIEWER_ID
    viewer = await fetch_one("SELECT id, name, team_id FROM people WHERE id = %s", (viewer_id,))
    if not viewer:
        raise HTTPException(404, f"viewer {viewer_id} not found")

    orgs = await fetch_all("SELECT id, name FROM orgs ORDER BY id")
    teams = await fetch_all("SELECT id, org_id, name, lead_id, summary FROM teams ORDER BY id")
    people = await fetch_all(f"""
        SELECT p.id, p.team_id, p.name, p.title,
               EXISTS (SELECT 1 FROM teams t WHERE t.lead_id = p.id) AS is_lead,
               p.manager_id IS NOT DISTINCT FROM %(viewer)s AS is_viewer_report,
               {needs_connection('p.id')} AS needs_connection,
               (SELECT json_build_object('id', t.id, 'summary', t.summary, 'created_at', t.created_at)
                FROM tasks t WHERE t.person_id = p.id AND t.status IN ('open', 'notified')
                ORDER BY t.created_at DESC LIMIT 1) AS open_task
        FROM people p ORDER BY p.id""", {"viewer": viewer_id, "amber_min": settings.AMBER_AFTER_MIN})

    edges = await fetch_all(f"""
        WITH {LATEST_CONN}
        SELECT s.person_a AS a, s.person_b AS b, s.score, LEAST(s.rank_a, s.rank_b) AS rank,
               {edge_state('l.status')} AS state, COALESCE(l.message_count, 0) AS message_count,
               l.id AS connection_id
        FROM similarities s LEFT JOIN latest l ON l.a = s.person_a AND l.b = s.person_b
        UNION ALL
        SELECT l.a, l.b, l.match_score, NULL, {edge_state('l.status')}, l.message_count, l.id
        FROM latest l
        WHERE l.status IN ('suggested', 'requested', 'accepted', 'active')
          AND NOT EXISTS (SELECT 1 FROM similarities s WHERE s.person_a = l.a AND s.person_b = l.b)
        ORDER BY a, b""")

    bridges = await fetch_all("""
        SELECT o.team_a, o.team_b, o.score,
               EXISTS (SELECT 1 FROM connections c
                       JOIN people r ON r.id = c.requester_id JOIN people h ON h.id = c.helper_id
                       WHERE c.status IN ('accepted', 'active')
                         AND ((r.team_id, h.team_id) = (o.team_a, o.team_b)
                           OR (r.team_id, h.team_id) = (o.team_b, o.team_a))) AS has_connection
        FROM team_overlaps o WHERE o.score >= %s ORDER BY o.score DESC""", (settings.BRIDGE_MIN_SCORE,))

    return {"viewer": viewer, "orgs": orgs, "teams": teams, "people": people, "edges": edges,
            "bridges": bridges, "generated_at": datetime.now(timezone.utc)}
