"""Skill <-> skill edges (spec §7.4). Rebuilt by the pipeline only.

Two skills co-occur when the same source event produced both, or the same person has evidence for
both within 14 days of each other. Each event and each person counts once, so one prolific person's
unrelated work can't build an edge alone. For each unordered skill pair:
    cooccur(a, b) = distinct events that produced both + distinct people with both within 14 days
    weight(a, b)  = min(1, cooccur / sqrt(count(a) · count(b)))       count = evidence rows per skill
Pairs with cooccur >= SKILL_EDGE_MIN_COOCCUR are kept, top 10 per skill, then merged with the
P13 'related' edges (origin 'muse', weight 0.5): weight = max, origin 'both' when both exist.
"""
import logging

from app.config import settings
from app.db import fetch_all, pool
from app.expertise.canonicalize import MUSE_EDGE_WEIGHT

log = logging.getLogger("musketeer.skill_graph")

COOCCUR_DAYS = 14
TOP_PER_SKILL = 10

_COOCCUR = """
WITH counts AS (SELECT skill_id, count(*) AS n FROM expertise_events GROUP BY skill_id),
pairs AS (
  SELECT a, b, count(DISTINCT e) FILTER (WHERE e IS NOT NULL) + count(DISTINCT p) AS cooccur
  FROM (
    SELECT x.skill_id AS a, y.skill_id AS b, x.person_id AS p,
           CASE WHEN x.source_event_id = y.source_event_id AND x.time = y.time
                THEN x.source_event_id::text || '@' || x.time::text END AS e
    FROM expertise_events x JOIN expertise_events y
      ON y.person_id = x.person_id AND x.skill_id < y.skill_id
     AND y.time BETWEEN x.time - make_interval(days => %(days)s) AND x.time + make_interval(days => %(days)s)
  ) hits
  GROUP BY 1, 2 HAVING count(DISTINCT e) FILTER (WHERE e IS NOT NULL) + count(DISTINCT p) >= %(min)s
), weighted AS (
  SELECT a, b, cooccur, LEAST(1.0, cooccur / sqrt(ca.n * cb.n)) AS weight
  FROM pairs JOIN counts ca ON ca.skill_id = a JOIN counts cb ON cb.skill_id = b
), ranked AS (
  SELECT *, row_number() OVER (PARTITION BY a ORDER BY weight DESC, b) AS ra,
            row_number() OVER (PARTITION BY b ORDER BY weight DESC, a) AS rb
  FROM weighted
)
SELECT a, b, cooccur, weight FROM ranked WHERE ra <= %(top)s OR rb <= %(top)s
"""


async def rebuild() -> int:
    """Recompute co-occurrence edges and merge them with the Muse edges. Returns the edge count."""
    cooc = {(r["a"], r["b"]): r for r in await fetch_all(_COOCCUR, {
        "days": COOCCUR_DAYS, "min": settings.SKILL_EDGE_MIN_COOCCUR, "top": TOP_PER_SKILL})}
    muse = {(r["skill_a"], r["skill_b"]) for r in await fetch_all(
        "SELECT skill_a, skill_b FROM skill_edges WHERE origin IN ('muse', 'both')")}

    rows = []
    for key in cooc.keys() | muse:
        c = cooc.get(key)
        weight = max(c["weight"] if c else 0.0, MUSE_EDGE_WEIGHT if key in muse else 0.0)
        origin = "both" if c and key in muse else "cooccur" if c else "muse"
        rows.append((*key, weight, c["cooccur"] if c else 0, origin))

    async with pool.connection() as conn:
        await conn.execute("DELETE FROM skill_edges")
        async with conn.cursor() as cur:
            await cur.executemany("""INSERT INTO skill_edges (skill_a, skill_b, weight, cooccur, origin)
                                     VALUES (%s, %s, %s, %s, %s)""", rows)
    log.info("skill graph: %d edges (%d co-occurrence, %d muse)", len(rows), len(cooc), len(muse))
    return len(rows)
