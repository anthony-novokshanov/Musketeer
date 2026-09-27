"""Decay model (spec §7.3): the demo temporal model.

strength(p, s, t) = Σ weight · confidence · 2^(-(t - tᵢ) / half_life(kind))   over tᵢ in (t - WINDOW, t]
level(p, s, t)    = 1 - exp(-strength / STRENGTH_SCALE)

person_skill_state is a materialized snapshot of this, recomputed per person on new evidence and for
everyone by the background refresh. Evidence (expertise_events) is never modified.
"""
import logging
import math
from datetime import datetime, timedelta

from app.config import settings
from app.db import fetch_all, pool

log = logging.getLogger("musketeer.decay")

PREV_OFFSET_DAYS = 30
MIN_LEVEL = 0.05
FOCUS_AREAS = 6


def half_life(kind: str) -> float:
    return settings.HALF_LIFE_HELPED_DAYS if kind == "helped" else settings.HALF_LIFE_DAYS


def strength(evidence: list[tuple[datetime, str, float, float]], t: datetime) -> float:
    """Pure-Python twin of the SQL below; evidence = [(time, kind, weight, confidence)]."""
    window = timedelta(days=settings.EXPERTISE_WINDOW_DAYS)
    return sum(w * c * 2 ** (-(t - ti).total_seconds() / 86400 / half_life(k))
               for ti, k, w, c in evidence if t - window < ti <= t)


def level(s: float) -> float:
    return 1 - math.exp(-s / settings.STRENGTH_SCALE)


def trend(strength_now: float, strength_prev: float, first_seen: datetime, t: datetime) -> str:
    if first_seen > t - timedelta(days=settings.NEW_SKILL_DAYS):
        return "new"
    if strength_now > 1.25 * strength_prev:
        return "rising"
    if strength_now < 0.6 * strength_prev:
        return "fading"
    return "steady"


# Contributions at t and at t - 30 days, each over its own window; then one row per (person, skill).
_STATE = """
WITH ev AS (
  SELECT person_id, skill_id, time, snippet, weight * confidence AS base,
         CASE WHEN evidence_kind = 'helped' THEN %(hl_helped)s ELSE %(hl)s END AS hl
  FROM expertise_events
  WHERE time <= %(t)s AND time > %(t)s - make_interval(days => %(window)s + %(prev)s)
    AND (%(pids)s::text[] IS NULL OR person_id = ANY(%(pids)s))
), c AS (
  SELECT *,
    CASE WHEN time > %(t)s - make_interval(days => %(window)s)
         THEN base * power(2, -extract(epoch FROM %(t)s - time) / 86400 / hl) ELSE 0 END AS now_c,
    CASE WHEN time <= %(t)s - make_interval(days => %(prev)s)
         THEN base * power(2, -extract(epoch FROM (%(t)s - make_interval(days => %(prev)s)) - time) / 86400 / hl)
         ELSE 0 END AS prev_c
  FROM ev
), agg AS (
  SELECT person_id, skill_id, sum(now_c) AS strength, sum(prev_c) AS strength_prev,
         min(time) AS first_seen, max(time) FILTER (WHERE now_c > 0) AS last_seen,
         count(*) FILTER (WHERE now_c > 0) AS evidence_count,
         (array_agg(snippet ORDER BY now_c DESC))[1] AS best_snippet
  FROM c GROUP BY person_id, skill_id
)
SELECT *, 1 - exp(-strength / %(scale)s) AS level,
  CASE WHEN first_seen > %(t)s - make_interval(days => %(new_days)s) THEN 'new'
       WHEN strength > 1.25 * strength_prev THEN 'rising'
       WHEN strength < 0.6 * strength_prev THEN 'fading'
       ELSE 'steady' END AS trend
FROM agg WHERE 1 - exp(-strength / %(scale)s) >= %(min_level)s
"""


async def recompute(person_ids: list[str] | None = None, t: datetime | None = None) -> int:
    """Rebuild person_skill_state for these people (None = everyone) as of t (default now).
    Returns the number of state rows written."""
    params = {"t": t or datetime.now().astimezone(), "pids": person_ids, "window": settings.EXPERTISE_WINDOW_DAYS,
              "prev": PREV_OFFSET_DAYS, "hl": settings.HALF_LIFE_DAYS, "hl_helped": settings.HALF_LIFE_HELPED_DAYS,
              "scale": settings.STRENGTH_SCALE, "new_days": settings.NEW_SKILL_DAYS, "min_level": MIN_LEVEL}
    async with pool.connection() as conn:   # one transaction: readers never see a half-built state
        if person_ids is None:
            await conn.execute("DELETE FROM person_skill_state")
        else:
            await conn.execute("DELETE FROM person_skill_state WHERE person_id = ANY(%s)", (person_ids,))
        cur = await conn.execute(f"""
            INSERT INTO person_skill_state (person_id, skill_id, strength, level, strength_prev, trend,
                first_seen, last_seen, evidence_count, best_snippet, computed_at)
            SELECT person_id, skill_id, strength, level, strength_prev, trend,
                   first_seen, last_seen, evidence_count, best_snippet, %(t)s
            FROM ({_STATE}) s""", params)
        written = cur.rowcount
        await _focus_areas(conn, person_ids)
    return written


async def recompute_person(person_id: str) -> int:
    return await recompute([person_id])


async def recompute_all() -> int:
    n = await recompute()
    log.info("recomputed person_skill_state: %d rows", n)
    return n


async def _focus_areas(conn, person_ids: list[str] | None) -> None:
    """people.focus_areas = top skills by level; teams.focus_areas = top skills by summed member level."""
    await conn.execute(f"""
        UPDATE people p SET focus_areas = COALESCE((
            SELECT array_agg(name ORDER BY level DESC, name) FROM (
                SELECT s.name, st.level FROM person_skill_state st JOIN skills s ON s.id = st.skill_id
                WHERE st.person_id = p.id ORDER BY st.level DESC, s.name LIMIT {FOCUS_AREAS}) top), '{{}}')
        WHERE %(pids)s::text[] IS NULL OR p.id = ANY(%(pids)s)""", {"pids": person_ids})
    await conn.execute(f"""
        UPDATE teams t SET focus_areas = COALESCE((
            SELECT array_agg(name ORDER BY total DESC, name) FROM (
                SELECT s.name, sum(st.level) AS total FROM person_skill_state st
                JOIN people p ON p.id = st.person_id JOIN skills s ON s.id = st.skill_id
                WHERE p.team_id = t.id GROUP BY s.name ORDER BY total DESC, s.name LIMIT {FOCUS_AREAS}) top), '{{}}')
        WHERE %(pids)s::text[] IS NULL
           OR t.id IN (SELECT team_id FROM people WHERE id = ANY(%(pids)s))""", {"pids": person_ids})


async def state(person_id: str) -> list[dict]:
    return await fetch_all("""SELECT st.*, s.name FROM person_skill_state st JOIN skills s ON s.id = st.skill_id
                              WHERE st.person_id = %s ORDER BY st.level DESC""", (person_id,))
