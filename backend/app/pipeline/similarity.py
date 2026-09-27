"""Person-pair edges (spec §9.3): rank everyone, symmetrize, blend with the temporal graph, store.

build_similarities (pipeline) calls Muse. refresh_temporal (background loop) never does: it re-scores the
stored pairs from their stored semantic_score and the current skill state, so new skills move edges
within minutes.
"""
import asyncio
import logging

from app.db import fetch_all, pool
from app.expertise import decay, vectors
from app.expertise.vectors import SkillSpace
from app.matching.base import get_matcher
from app.matching.scoring import finalize_reason, pair_score, rank_lists, symmetrize
from app.pipeline.profiles import engineer_dirs

log = logging.getLogger("musketeer.similarity")


def _score_pairs(pairs: dict[tuple[str, str], dict], space: SkillSpace, dirs: dict[str, set[str]]) -> None:
    for (a, b), pr in pairs.items():
        pr["temporal_score"] = space.temporal_sim(a, b)
        pr["dir_overlap"], pr["score"], pr["shared_dirs"] = pair_score(
            pr["semantic_score"], pr["temporal_score"], dirs.get(a, set()), dirs.get(b, set()))
        pr["shared_skills"] = space.shared_skills(a, b)
    rank_lists(pairs)


async def build_similarities() -> int:
    people = [p["id"] for p in await fetch_all("SELECT id FROM people ORDER BY id")]
    space, dirs = await vectors.load(), await engineer_dirs()
    matcher = get_matcher()
    ranked = await asyncio.gather(*(matcher.rank_for_person(p, space=space, dirs=dirs) for p in people))
    pairs = symmetrize(dict(zip(people, ranked)))
    _score_pairs(pairs, space, dirs)

    rows = [(a, b, pr["semantic_score"], pr["temporal_score"], pr["dir_overlap"], pr["score"], pr["rank_a"],
             pr["rank_b"], finalize_reason(pr["reason"], pr["shared_dirs"]), pr["shared_skills"], pr["shared_dirs"])
            for (a, b), pr in pairs.items()]
    async with pool.connection() as conn:  # one transaction: readers never see a half-built table
        await conn.execute("DELETE FROM similarities")
        async with conn.cursor() as cur:
            await cur.executemany("""
                INSERT INTO similarities (person_a, person_b, semantic_score, temporal_score, dir_overlap, score,
                                          rank_a, rank_b, reason, shared_skills, shared_dirs)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", rows)
    log.info("stored %d similarity edges", len(rows))
    return len(rows)


async def refresh_temporal() -> int:
    """Temporal-only refresh: recompute skill state, then temporal_score, score, ranks, shared skills
    for existing pairs from their stored semantic_score. No Muse calls."""
    await decay.recompute_all()
    space, dirs = await vectors.load(), await engineer_dirs()
    pairs = {(r["person_a"], r["person_b"]): {"semantic_score": r["semantic_score"],
                                              "listed_by_a": r["rank_a"] is not None,
                                              "listed_by_b": r["rank_b"] is not None}
             for r in await fetch_all("SELECT person_a, person_b, semantic_score, rank_a, rank_b FROM similarities")}
    _score_pairs(pairs, space, dirs)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany("""
                UPDATE similarities SET temporal_score = %s, score = %s, rank_a = %s, rank_b = %s,
                                        shared_skills = %s, updated_at = now()
                WHERE person_a = %s AND person_b = %s""",
                [(pr["temporal_score"], pr["score"], pr["rank_a"], pr["rank_b"], pr["shared_skills"], a, b)
                 for (a, b), pr in pairs.items()])
    log.info("temporal refresh: re-scored %d pairs", len(pairs))
    return len(pairs)


async def refresh_loop(minutes: float) -> None:
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            await refresh_temporal()
        except Exception:
            log.exception("temporal refresh failed")
