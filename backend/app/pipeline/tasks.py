"""Task pipeline (spec §9.4): received event -> detect task + required skills -> match -> maybe auto-suggest."""
import logging

from psycopg.types.json import Jsonb

from app.ai.muse import muse_json
from app.bot import flows
from app.config import settings
from app.connectors.base import insert_event
from app.db import execute, fetch_all, fetch_one
from app.matching import candidates
from app.matching.base import get_matcher
from app.matching.scoring import adjust_task_candidate
from app.schemas import DetectTaskOut, NormalizedEvent, RequiredSkillsOut

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
        "recipient_title": ev["person_title"], "recipient_team": ev["team"],
        "skills": await candidates.skill_names()}, DetectTaskOut)
    result = {"task_id": None, "detection": det.model_dump(), "candidates": [], "connection_id": None}
    if not (det.is_new_task and det.confidence >= MIN_TASK_CONFIDENCE):
        return result

    required = await candidates.resolve_required(det.required_skills)
    task = await fetch_one("""INSERT INTO tasks (person_id, source_event_id, source, summary, task_type, required_skills)
                              VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                           (ev["person_id"], ev["id"], ev["source"], det.summary, det.task_type, Jsonb(required)))
    result["task_id"] = task["id"]
    ranked = await rank_candidates(task["id"], ev["person_id"])
    result["candidates"] = ranked
    if not ranked:
        return result

    best = ranked[0]
    await execute("UPDATE tasks SET best_match_score = %s WHERE id = %s", (best["adjusted"], task["id"]))
    if best["adjusted"] >= settings.AUTO_NOTIFY_THRESHOLD:
        result["connection_id"] = await _suggest(task["id"], ev["person_id"], best)
    log.info("task %s for %s: best %s (%.2f) -> %s", task["id"], ev["person_id"], best["person_id"],
             best["adjusted"], "suggested" if result["connection_id"] else "open")
    return result


async def rank_candidates(task_id: int, requester_id: str) -> list[dict]:
    """Matcher candidates with the §9.4 adjustments (novelty, load, unhelpful), best first."""
    matches = await get_matcher().rank_for_task(task_id)
    if not matches:
        return []
    skill_ids = [r["skill_id"] for r in (await fetch_one("SELECT required_skills FROM tasks WHERE id = %s",
                                                         (task_id,)))["required_skills"]]
    info = {r["id"]: r for r in await fetch_all("""
        SELECT p.id, p.available,
               EXISTS (SELECT 1 FROM connections c WHERE p.id IN (c.requester_id, c.helper_id)
                                                     AND %(r)s IN (c.requester_id, c.helper_id)) AS interacted,
               (SELECT count(*) FROM connections c WHERE c.helper_id = p.id
                                                     AND c.created_at > now() - INTERVAL '7 days') AS recent_requests,
               EXISTS (SELECT 1 FROM match_feedback f WHERE f.helper_id = p.id AND f.rater_id = f.requester_id
                                                        AND NOT f.helpful AND f.time > now() - INTERVAL '30 days'
                                                        AND f.skill_ids && %(skills)s::text[]) AS unhelpful
        FROM people p WHERE p.id = ANY(%(ids)s)""",
        {"r": requester_id, "ids": [m.person_id for m in matches], "skills": skill_ids})}
    out = []
    for m in matches:
        p = info.get(m.person_id)
        if not p or not p["available"] or m.person_id == requester_id:
            continue
        out.append({"person_id": m.person_id, "score": m.score, "reason": m.reason,
                    "semantic_score": m.semantic_score, "temporal_score": m.temporal_score,
                    "adjusted": adjust_task_candidate(m.score, p["interacted"], p["recent_requests"], p["unhelpful"])})
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


async def backfill_required_skills() -> int:
    """Pipeline step 2: open tasks created without an event (the seeded amber task) get their skills
    from P1's required-skills part."""
    tasks = await fetch_all("""SELECT id, summary FROM tasks
                               WHERE status IN ('open', 'notified') AND required_skills = '[]'::jsonb""")
    names = await candidates.skill_names()
    for t in tasks:
        out = await muse_json("required_skills", {"text": t["summary"], "skills": names}, RequiredSkillsOut)
        await execute("UPDATE tasks SET required_skills = %s WHERE id = %s",
                      (Jsonb(await candidates.resolve_required(out.required_skills)), t["id"]))
    return len(tasks)
