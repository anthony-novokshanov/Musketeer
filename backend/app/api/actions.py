from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.bot import flows
from app.config import settings
from app.db import fetch_one

router = APIRouter()


class NudgeIn(BaseModel):
    viewer_id: str | None = None
    employee_id: str
    target_id: str


class IntroIn(BaseModel):
    viewer_id: str | None = None
    team_a: str
    team_b: str


class ActionOut(BaseModel):
    connection_id: int
    delivered: str


async def _ensure_no_open_connection(x: str, y: str) -> None:
    if await fetch_one("""SELECT 1 FROM connections WHERE status IN ('suggested', 'requested', 'accepted', 'active')
                          AND %s IN (requester_id, helper_id) AND %s IN (requester_id, helper_id)""", (x, y)):
        raise HTTPException(409, "these two already have an open connection")


async def _require_person(pid: str) -> None:
    if not await fetch_one("SELECT 1 FROM people WHERE id = %s", (pid,)):
        raise HTTPException(404, f"person {pid} not found")


@router.post("/actions/nudge", response_model=ActionOut)
async def nudge(body: NudgeIn):
    viewer = body.viewer_id or settings.DEFAULT_VIEWER_ID
    for pid in (viewer, body.employee_id, body.target_id):
        await _require_person(pid)
    await _ensure_no_open_connection(body.employee_id, body.target_id)
    sim = await fetch_one("""SELECT reason FROM similarities
                             WHERE person_a = LEAST(%(x)s, %(y)s) AND person_b = GREATEST(%(x)s, %(y)s)""",
                          {"x": body.employee_id, "y": body.target_id})
    # Attach the employee's open task so a successful connection clears their amber dot.
    task = await fetch_one("""SELECT id FROM tasks WHERE person_id = %s AND status IN ('open', 'notified')
                              ORDER BY created_at DESC LIMIT 1""", (body.employee_id,))
    conn_id, delivered = await flows.open_connection(
        body.employee_id, body.target_id, "manager_nudge", sim["reason"] if sim else "You do similar work.",
        task_id=task and task["id"], initiated_by=viewer)
    return {"connection_id": conn_id, "delivered": delivered}


@router.post("/actions/introduce-leads", response_model=ActionOut)
async def introduce_leads(body: IntroIn):
    viewer = body.viewer_id or settings.DEFAULT_VIEWER_ID
    await _require_person(viewer)
    overlap = await fetch_one("""
        SELECT o.summary, ta.lead_id AS lead_a, tb.lead_id AS lead_b
        FROM team_overlaps o JOIN teams ta ON ta.id = %(a)s JOIN teams tb ON tb.id = %(b)s
        WHERE o.team_a = LEAST(%(a)s, %(b)s) AND o.team_b = GREATEST(%(a)s, %(b)s)""",
        {"a": body.team_a, "b": body.team_b})
    if not overlap or not overlap["lead_a"] or not overlap["lead_b"]:
        raise HTTPException(404, f"no bridge between {body.team_a} and {body.team_b}")
    await _ensure_no_open_connection(overlap["lead_a"], overlap["lead_b"])
    conn_id, delivered = await flows.open_connection(
        overlap["lead_a"], overlap["lead_b"], "lead_intro", overlap["summary"], initiated_by=viewer)
    return {"connection_id": conn_id, "delivered": delivered}
