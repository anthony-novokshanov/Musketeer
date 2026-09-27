"""MATCHER_MODE=muse (spec §9.5): temporal candidates + the roster (expertise cards) go to Muse, which judges
and writes reasons; the final score blends Muse's judgment with the temporal graph (§9.3)."""
import re

from app.ai.muse import muse_json
from app.config import settings
from app.db import fetch_all, fetch_one
from app.expertise import vectors
from app.expertise.vectors import SkillSpace
from app.matching import candidates
from app.matching.base import Match
from app.matching.scoring import blend
from app.pipeline.profiles import engineer_dirs, expertise_cards, roster_block
from app.schemas import RankForPersonOut, RankForTaskOut, RequiredSkillsOut, ScoredPerson, SearchOut


def _to_matches(scored: list[ScoredPerson], valid_ids: set[str], exclude: str | None, limit: int,
                temporal: dict[str, float], shared: dict[str, list[str]] | None = None) -> list[Match]:
    """Drop hallucinated/excluded ids and duplicates, scale 0-100 -> 0-1, blend, sort, cap."""
    seen, out = set(), []
    for s in scored:
        if s.person_id in valid_ids and s.person_id != exclude and s.person_id not in seen:
            seen.add(s.person_id)
            semantic = max(0.0, min(1.0, s.score / 100))
            t = temporal.get(s.person_id, 0.0)
            out.append(Match(person_id=s.person_id, score=blend(semantic, t), semantic_score=semantic,
                             temporal_score=t, reason=s.reason, shared_skills=(shared or {}).get(s.person_id, [])))
    return sorted(out, key=lambda m: -m.score)[:limit]


def _ids(roster: str) -> set[str]:
    """Person ids at the start of lines ("[p_001] ...") in a roster or a candidate listing."""
    return set(re.findall(r"^\[(\w+)\]", roster, flags=re.MULTILINE))


async def _names() -> dict[str, str]:
    return {r["id"]: r["name"] for r in await fetch_all("SELECT id, name FROM people")}


def _candidate_lines(ranked: list[tuple[str, float]], names: dict[str, str], label: str,
                     space: SkillSpace | None = None, owner: str | None = None) -> str:
    lines = []
    for pid, value in ranked:
        line = f"[{pid}] {names.get(pid, '')} ({label} {value:.2f})"
        if space and owner:
            shared = [space.names[s] for s in space.shared_skills(owner, pid)[:3]]
            if shared:
                line += f"; shared recent skills: {', '.join(shared)}"
        lines.append(line)
    return "\n".join(lines) or "(none)"


class MuseMatcher:
    # Hooks HybridMatcher overrides (spec §8.6). Here: the whole roster, and no extra candidates.
    async def _roster(self, shortlist: list[str]) -> str:
        return await roster_block()

    async def _extra_for_person(self, person_id: str, pool: list[str], space: SkillSpace) -> str:
        return ""

    async def _extra_for_task(self, task_id: int, task: dict, available: set[str], pool: list[tuple[str, float]],
                              space: SkillSpace) -> list[tuple[str, float]]:
        return []

    async def _extra_for_query(self, query: str, pool: list[tuple[str, float]], space: SkillSpace,
                               required: list[dict]) -> str:
        return ""

    async def rank_for_person(self, person_id: str, space: SkillSpace | None = None,
                              dirs: dict[str, set[str]] | None = None) -> list[Match]:
        space = space or await vectors.load()
        dirs = dirs if dirs is not None else await engineer_dirs()
        pool = candidates.for_person(space, person_id, dirs)
        listing = _candidate_lines([(p, space.temporal_sim(person_id, p)) for p in pool], await _names(),
                                   "temporal similarity", space, person_id)
        extra = await self._extra_for_person(person_id, pool, space)
        listing += extra
        roster = await self._roster([person_id, *pool, *_ids(extra)])
        out = await muse_json("rank_for_person", {"target_id": person_id, "topk": settings.TOPK_STORE,
                                                  "candidates": listing}, RankForPersonOut, prefix=roster)
        ids = _ids(roster)
        temporal = {p: space.temporal_sim(person_id, p) for p in ids}
        shared = {p: space.shared_skills(person_id, p) for p in ids}
        return _to_matches(out.matches, ids, person_id, settings.TOPK_STORE, temporal, shared)

    async def rank_for_task(self, task_id: int) -> list[Match]:
        task = await fetch_one("SELECT person_id, summary, required_skills FROM tasks WHERE id = %s", (task_id,))
        requester, required = task["person_id"], task["required_skills"]
        space = await vectors.load()
        available = {r["id"] for r in await fetch_all("SELECT id FROM people WHERE available AND id <> %s",
                                                      (requester,))}
        if required:
            pool = [(p, r) for p, r in space.rank_for_skills(required) if p in available][:settings.CANDIDATE_POOL]
        else:   # no skills detected: let Muse judge everyone (demo scale)
            pool = [(p, 0.0) for p in sorted(available)]
        pool += await self._extra_for_task(task_id, task, available, pool, space)
        if not pool:
            return []
        cards = await expertise_cards([p for p, _ in pool])
        blocks = []
        for pid, relevance in pool:
            matching = ", ".join(f"{space.names.get(r['skill_id'], r['skill_id'])} {space.cover(pid, r['skill_id']):.2f}"
                                 for r in required)
            blocks.append(f"{cards[pid]}\ntask_relevance: {relevance:.2f}" + (f" (per skill: {matching})" if matching else ""))
        skills = ", ".join(f"{space.names.get(r['skill_id'], r['skill_id'])} ({r['weight']:.1f})" for r in required)
        out = await muse_json("rank_for_task", {"requester_id": requester, "task_summary": task["summary"],
                                                "required_skills": skills or "(none detected)",
                                                "candidates": "\n\n".join(blocks)}, RankForTaskOut)
        temporal = dict(pool)
        return _to_matches(out.candidates, set(temporal), requester, 3, temporal)

    async def search(self, query: str) -> list[Match]:
        req = await muse_json("required_skills", {"text": query, "skills": await candidates.skill_names()},
                              RequiredSkillsOut, cache=False)
        required = await candidates.resolve_required(req.required_skills)
        space = await vectors.load()
        pool = candidates.for_skills(space, required)
        extra = await self._extra_for_query(query, pool, space, required)
        roster = await self._roster([p for p, _ in pool] + sorted(_ids(extra)))
        out = await muse_json("search", {"query": query,
                                         "candidates": _candidate_lines(pool, await _names(), "relevance") + extra},
                              SearchOut, prefix=roster, cache=False)
        ids = _ids(roster)
        temporal = {p: space.task_relevance(p, required) for p in ids}
        return _to_matches(out.results, ids, None, 5, temporal)
