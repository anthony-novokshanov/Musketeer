"""Person-pair edges (spec §8.3): rank everyone, symmetrize, add code overlap, store."""
import asyncio
import logging

from app.db import fetch_all, pool
from app.matching.base import get_matcher
from app.matching.scoring import finalize_reason, pair_score, symmetrize
from app.pipeline.profiles import github_aggregates

log = logging.getLogger("musketeer.similarity")


async def build_similarities() -> int:
    people = await fetch_all("SELECT id, is_engineer FROM people ORDER BY id")
    matcher = get_matcher()
    ranked = await asyncio.gather(*(matcher.rank_for_person(p["id"]) for p in people))
    pairs = symmetrize({p["id"]: r for p, r in zip(people, ranked)})

    gh = await github_aggregates()
    dirs = {p["id"]: gh[p["id"]]["all_directories"] if p["is_engineer"] and p["id"] in gh else set()
            for p in people}

    rows = []
    for (a, b), pr in pairs.items():
        dir_overlap, score, shared = pair_score(pr["semantic_score"], dirs[a], dirs[b])
        rows.append((a, b, pr["semantic_score"], dir_overlap, score, pr["rank_a"], pr["rank_b"],
                     finalize_reason(pr["reason"], shared), shared))

    async with pool.connection() as conn:  # one transaction: readers never see a half-built table
        await conn.execute("DELETE FROM similarities")
        async with conn.cursor() as cur:
            await cur.executemany("""
                INSERT INTO similarities (person_a, person_b, semantic_score, dir_overlap, score,
                                          rank_a, rank_b, reason, shared_dirs)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""", rows)
    log.info("stored %d similarity edges", len(rows))
    return len(rows)
