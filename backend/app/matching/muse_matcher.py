"""MATCHER_MODE=muse (spec §8.5): the whole roster goes to Muse as a cached prefix."""
from app.ai.muse import muse_json
from app.config import settings
from app.db import fetch_one
from app.matching.base import Match
from app.pipeline.profiles import roster_block
from app.schemas import RankForPersonOut, RankForTaskOut, ScoredPerson, SearchOut


def _to_matches(scored: list[ScoredPerson], valid_ids: set[str], exclude: str | None, limit: int) -> list[Match]:
    """Drop hallucinated/excluded ids and duplicates, scale 0-100 -> 0-1, sort, cap."""
    seen, out = set(), []
    for s in sorted(scored, key=lambda s: -s.score):
        if s.person_id in valid_ids and s.person_id != exclude and s.person_id not in seen:
            seen.add(s.person_id)
            out.append(Match(person_id=s.person_id, score=max(0.0, min(1.0, s.score / 100)), reason=s.reason))
    return out[:limit]


def _ids(roster: str) -> set[str]:
    return {line[1:line.index("]")] for line in roster.splitlines()}


class MuseMatcher:
    async def rank_for_person(self, person_id: str) -> list[Match]:
        roster = await roster_block()
        out = await muse_json("rank_for_person", {"target_id": person_id, "topk": settings.TOPK_STORE},
                              RankForPersonOut, prefix=roster)
        return _to_matches(out.matches, _ids(roster), person_id, settings.TOPK_STORE)

    async def rank_for_task(self, task_id: int) -> list[Match]:
        task = await fetch_one("SELECT person_id, summary FROM tasks WHERE id = %s", (task_id,))
        roster = await roster_block()
        out = await muse_json("rank_for_task", {"requester_id": task["person_id"], "task_summary": task["summary"]},
                              RankForTaskOut, prefix=roster)
        return _to_matches(out.candidates, _ids(roster), task["person_id"], 3)

    async def search(self, query: str) -> list[Match]:
        roster = await roster_block()
        out = await muse_json("search", {"query": query}, SearchOut, prefix=roster, cache=False)
        return _to_matches(out.results, _ids(roster), None, 5)
