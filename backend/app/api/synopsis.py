import json
import logging
import time

from fastapi import APIRouter

from app.ai.muse import muse_json
from app.config import settings
from app.db import fetch_all, fetch_one
from app.schemas import SynopsisOut, SynopsisSummaryOut

log = logging.getLogger("musketeer.synopsis")
router = APIRouter()

SUMMARY_TTL_SEC = 600
_summary_cache: dict[int, tuple[float, str]] = {}


async def _summary(days: int, body: dict) -> str:
    """P11 on the stats, cached 10 minutes. Falls back to a plain sentence if Muse is unavailable."""
    hit = _summary_cache.get(days)
    if hit and time.monotonic() - hit[0] < SUMMARY_TTL_SEC:
        return hit[1]
    try:
        out = await muse_json("synopsis_summary", {"stats": json.dumps(body, default=str)}, SynopsisSummaryOut,
                              cache=False)
        summary = out.summary
    except Exception as e:
        log.warning("synopsis summary unavailable: %s", e)
        return f"{body['stats']['connections_made']} connections made in the last {days} days."
    _summary_cache[days] = (time.monotonic(), summary)
    return summary


@router.get("/synopsis", response_model=SynopsisOut)
async def synopsis(days: int = 30, viewer_id: str | None = None):
    # Org-wide for every manager (spec §15 default); viewer_id accepted for API symmetry.
    window = {"days": days}
    stats = await fetch_one("""
        WITH accepted AS (
          SELECT c.*, r.team_id <> h.team_id AS cross_team
          FROM connections c JOIN people r ON r.id = c.requester_id JOIN people h ON h.id = c.helper_id
          WHERE c.accepted_at > now() - make_interval(days => %(days)s))
        SELECT
          (SELECT count(*) FROM accepted) AS connections_made,
          (SELECT avg(value::int) FROM connection_events
            WHERE event = 'feedback' AND value IS NOT NULL AND time > now() - make_interval(days => %(days)s))
            AS helpful_rate,
          (SELECT avg(cross_team::int) FROM accepted) AS cross_team_share,
          (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM a.accepted_at - t.created_at) / 60)
            FROM accepted a JOIN tasks t ON t.id = a.task_id WHERE a.origin = 'auto') AS median_minutes_to_connect
        """, window)
    stats = {k: (round(float(v), 3) if v is not None and k != "connections_made" else v) for k, v in stats.items()}

    missed = await fetch_all("""
        SELECT s.person_a AS a, s.person_b AS b, pa.name AS a_name, pb.name AS b_name,
               ta.name AS a_team, tb.name AS b_team, s.score, s.reason
        FROM similarities s
        JOIN people pa ON pa.id = s.person_a JOIN people pb ON pb.id = s.person_b
        JOIN teams ta ON ta.id = pa.team_id JOIN teams tb ON tb.id = pb.team_id
        WHERE s.score >= %s AND pa.team_id <> pb.team_id AND NOT EXISTS (
          SELECT 1 FROM connections c WHERE LEAST(c.requester_id, c.helper_id) = s.person_a
                                        AND GREATEST(c.requester_id, c.helper_id) = s.person_b)
        ORDER BY s.score DESC LIMIT 5""", (settings.MISSED_MIN_SCORE,))

    teams_should_talk = await fetch_all("""
        SELECT o.team_a, o.team_b, ta.name AS team_a_name, tb.name AS team_b_name, o.score, o.summary
        FROM team_overlaps o JOIN teams ta ON ta.id = o.team_a JOIN teams tb ON tb.id = o.team_b
        WHERE o.score >= %s AND NOT EXISTS (
          SELECT 1 FROM connections c JOIN people r ON r.id = c.requester_id JOIN people h ON h.id = c.helper_id
          WHERE c.status IN ('accepted', 'active')
            AND (r.team_id, h.team_id) IN ((o.team_a, o.team_b), (o.team_b, o.team_a)))
        ORDER BY o.score DESC LIMIT 3""", (settings.BRIDGE_MIN_SCORE,))

    trend = await fetch_all("""
        SELECT to_char(d, 'YYYY-MM-DD') AS day,
               COALESCE(sum(n) FILTER (WHERE event = 'suggested'), 0)::int AS suggested,
               COALESCE(sum(n) FILTER (WHERE event = 'accepted'), 0)::int AS accepted
        FROM generate_series(date_trunc('day', now()) - make_interval(days => %(days)s - 1),
                             date_trunc('day', now()), INTERVAL '1 day') d
        LEFT JOIN connection_daily cd ON cd.day = d
        GROUP BY d ORDER BY d""", window)

    body = {"stats": stats, "missed_opportunities": missed, "teams_should_talk": teams_should_talk, "trend": trend}
    return {"summary": await _summary(days, body), **body}
