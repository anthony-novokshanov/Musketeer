"""Dev routes (ENABLE_DEV_ROUTES only): demo fallback and rehearsal reset."""
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.bot import flows
from app.db import fetch_all, fetch_one, pool
from app.expertise import decay
from app.expertise.extract import ingest_did
from app.pipeline import similarity
from app.pipeline.tasks import ingest_received
from app.schemas import NormalizedEvent

router = APIRouter()


class SimulateIn(BaseModel):
    person_id: str
    source: Literal["email", "slack"] = "email"
    direction: Literal["received", "did"] = "received"
    title: str = ""
    text: str


@router.post("/dev/simulate-event")
async def simulate_event(body: SimulateIn):
    """Runs the exact pipeline of a real incoming event: 'received' -> task detection and matching,
    'did' -> expertise extraction (returns the evidence written)."""
    if not await fetch_one("SELECT 1 FROM people WHERE id = %s", (body.person_id,)):
        raise HTTPException(404, f"person {body.person_id} not found")
    ev = NormalizedEvent(
        source=body.source, kind="email" if body.source == "email" else "slack_message",
        external_id=f"sim-{uuid.uuid4()}", time=datetime.now(timezone.utc), person_id=body.person_id,
        direction=body.direction, title=body.title or None, text=body.text[:800], metadata={"simulated": True})
    if body.direction == "did":
        return {"evidence": await ingest_did(ev)}
    return await ingest_received(ev)


@router.get("/dev/expertise/{person_id}")
async def expertise(person_id: str):
    """Full skill state with trends plus the last 20 evidence rows. Debug only; never used by the UI."""
    if not await fetch_one("SELECT 1 FROM people WHERE id = %s", (person_id,)):
        raise HTTPException(404, f"person {person_id} not found")
    return {"state": await decay.state(person_id),
            "evidence": await fetch_all("""SELECT x.time, x.skill_id, s.name AS skill_name, x.evidence_kind, x.weight,
                                                  x.confidence, x.source, x.snippet
                                           FROM expertise_events x JOIN skills s ON s.id = x.skill_id
                                           WHERE x.person_id = %s ORDER BY x.time DESC LIMIT 20""", (person_id,))}


@router.post("/dev/refresh")
async def refresh():
    """Run the temporal-only refresh now (no Muse calls)."""
    return {"pairs": await similarity.refresh_temporal()}


@router.post("/dev/reset-demo")
async def reset_demo():
    """Delete everything the live demo created; restore the amber-case task. Seed history is kept."""
    flows.cancel_background()
    async with pool.connection() as conn:
        await conn.execute("""DELETE FROM connection_events WHERE connection_id IN
                              (SELECT id FROM connections WHERE NOT is_seed)""")
        await conn.execute("DELETE FROM match_feedback WHERE connection_id IN (SELECT id FROM connections WHERE NOT is_seed)")
        await conn.execute("DELETE FROM expertise_events WHERE source = 'feedback'")
        await conn.execute("DELETE FROM connections WHERE NOT is_seed")
        # Live tasks come from events; seed-history tasks and the amber task have no source event.
        await conn.execute("DELETE FROM tasks WHERE source_event_id IS NOT NULL")
        cur = await conn.execute("""
            UPDATE tasks SET status = 'open', best_match_score = NULL, created_at = now() - INTERVAL '2 days'
            WHERE source_event_id IS NULL AND id NOT IN (SELECT task_id FROM connections WHERE task_id IS NOT NULL)""")
    restored = cur.rowcount
    await decay.recompute_all()
    return {"ok": True, "amber_tasks_restored": restored}
