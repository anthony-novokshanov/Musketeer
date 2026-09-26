"""Dev routes (ENABLE_DEV_ROUTES only): demo fallback and rehearsal reset."""
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.bot import flows
from app.db import fetch_one, pool
from app.pipeline.tasks import ingest_received
from app.schemas import NormalizedEvent

router = APIRouter()


class SimulateIn(BaseModel):
    person_id: str
    source: Literal["email"] = "email"
    title: str
    text: str


@router.post("/dev/simulate-event")
async def simulate_event(body: SimulateIn):
    """Runs the exact pipeline of a real incoming email."""
    if not await fetch_one("SELECT 1 FROM people WHERE id = %s", (body.person_id,)):
        raise HTTPException(404, f"person {body.person_id} not found")
    return await ingest_received(NormalizedEvent(
        source=body.source, kind="email", external_id=f"sim-{uuid.uuid4()}", time=datetime.now(timezone.utc),
        person_id=body.person_id, direction="received", title=body.title, text=body.text[:800],
        metadata={"simulated": True}))


@router.post("/dev/reset-demo")
async def reset_demo():
    """Delete everything the live demo created; restore the amber-case task. Seed history is kept."""
    flows.cancel_background()
    async with pool.connection() as conn:
        await conn.execute("""DELETE FROM connection_events WHERE connection_id IN
                              (SELECT id FROM connections WHERE NOT is_seed)""")
        await conn.execute("DELETE FROM connections WHERE NOT is_seed")
        # Live tasks come from events; seed-history tasks and the amber task have no source event.
        await conn.execute("DELETE FROM tasks WHERE source_event_id IS NOT NULL")
        cur = await conn.execute("""
            UPDATE tasks SET status = 'open', best_match_score = NULL, created_at = now() - INTERVAL '2 days'
            WHERE source_event_id IS NULL AND id NOT IN (SELECT task_id FROM connections WHERE task_id IS NOT NULL)""")
    return {"ok": True, "amber_tasks_restored": cur.rowcount}
