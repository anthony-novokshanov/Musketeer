"""Task pipeline (spec §8.4): received event -> detect task -> match -> maybe auto-suggest."""
import logging

from app.ai.muse import muse_json
from app.bot import flows
from app.config import settings
from app.connectors.base import insert_event
from app.db import execute, fetch_all, fetch_one
from app.matching.base import get_matcher
from app.matching.scoring import adjust_task_candidate
from app.schemas import DetectTaskOut, NormalizedEvent

log = logging.getLogger("musketeer.tasks")

MIN_TASK_CONFIDENCE = 0.7


async def ingest_received(ev: NormalizedEvent) -> dict | None:
    """Store a 'received' event and run the pipeline on it. None if it was a duplicate."""
    event_id = await insert_event(ev)
    return None if event_id is None else await process_event(event_id)


async def process_event(event_id: int) -> dict:
    ev = await fetch_one("""
        SELECT e.id, e.person_id, e.source, e.title, e.text, p.name, p.title AS person_title, t.name AS team
        FROM activity_events e JOIN people p ON p.id = e.person_id JOIN teams t ON t.id = p.team_id
        WHERE e.id = %s""", (event_id,))
    det = await muse_json("detect_task", {
        "title": ev["title"] or "", "text": ev["text"], "recipient_name": ev["name"],
        "recipient_title": ev["person_title"], "recipient_team": ev["team"]}, DetectTaskOut)
    result = {"task_id": None, "detection": det.model_dump(), "candidates": [], "connection_id": None}
    if not (det.is_new_task and det.confidence >= MIN_TASK_CONFIDENCE):
        return result

    task = await fetch_one("""INSERT INTO tasks (person_id, source_event_id, source, summary, task_type)
                              VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                           (ev["person_id"], ev["id"], ev["source"], det.summary, det.task_type))
    result["task_id"] = task["id"]
    candidates = await rank_candidates(task["id"], ev["person_id"])
    result["candidates"] = candidates
    if not candidates:
        return result

    best = candidates[0]
    await execute("UPDATE tasks SET best_match_score = %s WHERE id = %s", (best["adjusted"], task["id"]))
    if best["adjusted"] >= settings.AUTO_NOTIFY_THRESHOLD:
        result["connection_id"] = await _suggest(task["id"], ev["person_id"], best)
    log.info("task %s for %s: best %s (%.2f) -> %s", task["id"], ev["person_id"], best["person_id"],
             best["adjusted"], "suggested" if result["connection_id"] else "open")
    return result


async def rank_candidates(task_id: int, requester_id: str) -> list[dict]:
    """Matcher candidates with the §8.4 step-4 adjustments, best first."""
    matches = await get_matcher().rank_for_task(task_id)
    if not matches:
        return []
    info = {r["id"]: r for r in await fetch_all("""
        SELECT p.id, p.available,
               EXISTS (SELECT 1 FROM connections c WHERE p.id IN (c.requester_id, c.helper_id)
                                                     AND %(r)s IN (c.requester_id, c.helper_id)) AS interacted,
               (SELECT count(*) FROM connections c WHERE c.helper_id = p.id
                                                     AND c.created_at > now() - INTERVAL '7 days') AS recent_requests
        FROM people p WHERE p.id = ANY(%(ids)s)""", {"r": requester_id, "ids": [m.person_id for m in matches]})}
    out = []
    for m in matches:
        p = info.get(m.person_id)
        if not p or not p["available"] or m.person_id == requester_id:
            continue
        out.append({"person_id": m.person_id, "score": m.score, "reason": m.reason,
                    "adjusted": adjust_task_candidate(m.score, p["interacted"], p["recent_requests"])})
    return sorted(out, key=lambda c: -c["adjusted"])


async def _suggest(task_id: int, requester_id: str, candidate: dict) -> int:
    conn_id, _ = await flows.open_connection(requester_id, candidate["person_id"], "auto", candidate["reason"],
                                             match_score=candidate["adjusted"], task_id=task_id)
    await execute("UPDATE tasks SET status = 'notified' WHERE id = %s", (task_id,))
    return conn_id


async def retry_next_candidate(task_id: int) -> int | None:
    """After a helper declines: try the next candidate once if it clears the threshold; else task -> open."""
    task = await fetch_one("SELECT person_id FROM tasks WHERE id = %s", (task_id,))
    tried = {r["helper_id"] for r in await fetch_all("SELECT helper_id FROM connections WHERE task_id = %s", (task_id,))}
    if len(tried) == 1:
        nxt = next((c for c in await rank_candidates(task_id, task["person_id"])
                    if c["person_id"] not in tried and c["adjusted"] >= settings.AUTO_NOTIFY_THRESHOLD), None)
        if nxt:
            return await _suggest(task_id, task["person_id"], nxt)
    await execute("UPDATE tasks SET status = 'open' WHERE id = %s", (task_id,))
    return None
