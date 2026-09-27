"""Connection outcomes -> new evidence (spec §7.6).

Every rating is a match_feedback row (labels for tuning and the learned model). Once the requester has
rated, apply() writes evidence exactly once per connection:
  helpful   -> helper gets 'helped' (FEEDBACK_HELPED_WEIGHT, slow half-life), requester gets 'learned' (0.4)
  not really -> no evidence; scoring.py penalizes this helper on overlapping skills for 30 days
Seed-history connections go through the same function with source='seed_feedback', backdated.
"""
import logging

from app.config import settings
from app.db import execute, fetch_all, fetch_one, pool
from app.expertise import decay, vectors

log = logging.getLogger("musketeer.feedback")

LEARNED_WEIGHT = 0.4


async def skill_ids_for(conn: dict) -> list[str]:
    """The task's required skills; for nudges, intros and skill-less tasks, the pair's shared skills."""
    if conn["task_id"]:
        task = await fetch_one("SELECT required_skills FROM tasks WHERE id = %s", (conn["task_id"],))
        if task and task["required_skills"]:
            return [r["skill_id"] for r in task["required_skills"]]
    a, b = sorted((conn["requester_id"], conn["helper_id"]))
    row = await fetch_one("SELECT shared_skills FROM similarities WHERE person_a = %s AND person_b = %s", (a, b))
    if row and row["shared_skills"]:
        return row["shared_skills"]
    return (await vectors.load()).shared_skills(a, b)


async def record_rating(conn_id: int, rater_id: str, helpful: bool, time=None) -> None:
    conn = await fetch_one("SELECT * FROM connections WHERE id = %s", (conn_id,))
    await _insert_rating(conn, rater_id, helpful, await skill_ids_for(conn), time)


async def _insert_rating(conn: dict, rater_id: str, helpful: bool, skill_ids: list[str], time=None) -> None:
    async with pool.connection() as c:
        await c.execute("""INSERT INTO match_feedback (time, connection_id, requester_id, helper_id, rater_id,
                                                       helpful, skill_ids)
                           VALUES (COALESCE(%s, now()), %s, %s, %s, %s, %s, %s)""",
                        (time, conn["id"], conn["requester_id"], conn["helper_id"], rater_id, helpful, skill_ids))


async def apply(conn_id: int, source: str = "feedback", time=None) -> int:
    """Write evidence for a rated connection once. Returns the number of evidence rows written."""
    async with pool.connection() as c:   # claim it atomically so the loop and the button never double-apply
        claimed = await (await c.execute("""UPDATE connections SET feedback_applied = true
                                             WHERE id = %s AND NOT feedback_applied AND helpful_requester IS NOT NULL
                                             RETURNING *""", (conn_id,))).fetchone()
    if not claimed or not claimed["helpful_requester"]:
        return 0
    skills = await fetch_all("SELECT id, name FROM skills WHERE id = ANY(%s)", (await skill_ids_for(claimed),))
    rows = []
    for s in skills:
        rows.append((claimed["helper_id"], s["id"], "helped", settings.FEEDBACK_HELPED_WEIGHT,
                     f"helped a colleague with {s['name']}"))
        rows.append((claimed["requester_id"], s["id"], "learned", LEARNED_WEIGHT,
                     f"learned {s['name']} from a colleague"))
    async with pool.connection() as c:
        async with c.cursor() as cur:
            await cur.executemany("""
                INSERT INTO expertise_events (time, person_id, skill_id, evidence_kind, weight, confidence,
                                              source, connection_id, snippet)
                VALUES (COALESCE(%s, now()), %s, %s, %s, %s, 1.0, %s, %s, %s)""",
                [(time, p, sid, kind, w, source, conn_id, snip[:120]) for p, sid, kind, w, snip in rows])
    await decay.recompute([claimed["helper_id"], claimed["requester_id"]])
    return len(rows)


async def apply_pending() -> int:
    """Feedback loop: live connections rated since the last pass."""
    due = await fetch_all("""SELECT id FROM connections
                             WHERE NOT is_seed AND NOT feedback_applied AND helpful_requester IS NOT NULL""")
    return sum([await apply(c["id"]) for c in due])


async def apply_seed() -> int:
    """Pipeline: seed-history ratings -> match_feedback + evidence, backdated to when they were given."""
    conns = await fetch_all("""SELECT * FROM connections WHERE is_seed AND NOT feedback_applied
                               AND (helpful_requester IS NOT NULL OR helpful_helper IS NOT NULL)""")
    written = 0
    for conn in conns:
        ratings = await fetch_all("""SELECT time, person_id, value FROM connection_events
                                     WHERE connection_id = %s AND event = 'feedback' ORDER BY time""", (conn["id"],))
        skill_ids = await skill_ids_for(conn)
        for r in ratings:
            await _insert_rating(conn, r["person_id"], r["value"], skill_ids, r["time"])
        when = next((r["time"] for r in ratings if r["person_id"] == conn["requester_id"]),
                    conn["closed_at"] or conn["active_at"])
        written += await apply(conn["id"], source="seed_feedback", time=when)
        await execute("UPDATE connections SET feedback_applied = true WHERE id = %s", (conn["id"],))  # rated by helper only
    log.info("seed feedback: %d connections -> %d evidence rows", len(conns), written)
    return written
