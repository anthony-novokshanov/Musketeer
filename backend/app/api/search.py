from fastapi import APIRouter, HTTPException

from app.db import fetch_all
from app.matching.base import get_matcher
from app.schemas import SearchResultsOut

router = APIRouter()


@router.get("/search", response_model=SearchResultsOut)
async def search(q: str, viewer_id: str | None = None):
    q = q.strip()
    if not q:
        return {"results": []}
    try:
        matches = await get_matcher().search(q)
    except Exception as e:
        raise HTTPException(502, f"search failed: {e}")
    people = {r["id"]: r for r in await fetch_all("""
        SELECT p.id, p.name, t.id AS team_id, t.name AS team_name, t.org_id
        FROM people p JOIN teams t ON t.id = p.team_id WHERE p.id = ANY(%s)""", ([m.person_id for m in matches],))}
    return {"results": [{"person_id": m.person_id, **{k: v for k, v in people[m.person_id].items() if k != "id"},
                         "score": m.score, "reason": m.reason} for m in matches]}
