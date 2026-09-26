"""Team bridges (P8) and lead briefs (P9)."""
import asyncio
import logging

from app.ai.muse import muse_json
from app.config import settings
from app.db import fetch_all, pool
from app.schemas import LeadBriefOut, TeamOverlapsOut

log = logging.getLogger("musketeer.team_overlap")


def _describe(t: dict) -> str:
    return f"[{t['id']}] {t['name']}: {t['summary'] or ''} | Focus: {', '.join(t['focus_areas'])}"


async def build_team_overlaps() -> int:
    teams = {t["id"]: t for t in await fetch_all("SELECT id, name, summary, focus_areas FROM teams ORDER BY id")}
    out = await muse_json("team_overlaps", {"teams": "\n".join(_describe(t) for t in teams.values())}, TeamOverlapsOut)

    best: dict[tuple[str, str], dict] = {}
    for p in out.pairs:
        if p.team_a not in teams or p.team_b not in teams or p.team_a == p.team_b:
            continue
        key = tuple(sorted((p.team_a, p.team_b)))
        score = max(0.0, min(1.0, p.score / 100))
        if key not in best or score > best[key]["score"]:
            best[key] = {"score": score, "shared_topics": p.shared_topics[:5], "summary": p.summary}

    async def brief(key: tuple[str, str]) -> str | None:
        if best[key]["score"] < settings.BRIDGE_MIN_SCORE:
            return None
        a, b = key
        res = await muse_json("lead_brief", {"team_a": _describe(teams[a]), "team_b": _describe(teams[b]),
                                             "overlap_summary": best[key]["summary"]}, LeadBriefOut)
        return res.brief

    briefs = await asyncio.gather(*(brief(k) for k in best))
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM team_overlaps")
        async with conn.cursor() as cur:
            await cur.executemany("""
                INSERT INTO team_overlaps (team_a, team_b, score, shared_topics, summary, lead_brief)
                VALUES (%s, %s, %s, %s, %s, %s)""",
                [(a, b, v["score"], v["shared_topics"], v["summary"], lb) for ((a, b), v), lb in zip(best.items(), briefs)])
    log.info("stored %d team overlaps", len(best))
    return len(best)
