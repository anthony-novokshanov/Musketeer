"""MATCHER_MODE=hybrid (spec §8.6): Meta FAIR Contriever retrieval + the temporal graph, then Muse judges.

What changes from muse mode, and why it scales:
- Muse sees only a shortlist's expertise cards, never the whole roster, so prompt size stays flat
  however many people the company has.
- Candidates = the temporal pool (skills) plus up to EMBED_POOL people found by meaning (Contriever
  vectors in pgvector with a StreamingDiskANN index), so wording the skill graph missed can still surface.
  The temporal pool is always kept: hybrid only adds recall.
- Contriever cosines only order candidates. Every threshold still applies to Muse-blended scores.
"""
from app.ai import embedder
from app.config import settings
from app.db import execute, fetch_all
from app.expertise.vectors import SkillSpace
from app.matching.muse_matcher import MuseMatcher, _candidate_lines, _names
from app.pipeline.profiles import expertise_cards

HALF_LIFE_DAYS = 45   # recency weight on a work item that matches a task (spec §8.6)


async def nearest_people(vec: list[float], exclude: set[str], k: int) -> list[tuple[str, float]]:
    """People whose profile embedding is closest (ANN over people.summary_embedding)."""
    rows = await fetch_all("""
        SELECT id, 1 - (summary_embedding <=> %(q)s::vector) AS sim FROM people
        WHERE summary_embedding IS NOT NULL AND available AND NOT (id = ANY(%(ex)s))
        ORDER BY summary_embedding <=> %(q)s::vector LIMIT %(k)s""",
        {"q": embedder.literal(vec), "ex": list(exclude), "k": k})
    return [(r["id"], float(r["sim"])) for r in rows]


async def nearest_work(vec: list[float], exclude: set[str], k: int) -> list[tuple[str, float]]:
    """People with recent work closest to the query: ANN over work items first (index-friendly),
    then each person's best match weighted by recency."""
    rows = await fetch_all("""
        WITH near AS (
            SELECT person_id, time, 1 - (embedding <=> %(q)s::vector) AS sim FROM activity_events
            WHERE direction = 'did' AND embedding IS NOT NULL
            ORDER BY embedding <=> %(q)s::vector LIMIT %(n)s)
        SELECT n.person_id, max(n.sim * exp(-extract(epoch FROM now() - n.time) / 86400 / %(hl)s)) AS s
        FROM near n JOIN people p ON p.id = n.person_id
        WHERE p.available AND NOT (n.person_id = ANY(%(ex)s))
        GROUP BY n.person_id ORDER BY s DESC LIMIT %(k)s""",
        {"q": embedder.literal(vec), "n": settings.EMBED_EVENT_NEIGHBORS, "hl": HALF_LIFE_DAYS,
         "ex": list(exclude), "k": k})
    return [(r["person_id"], float(r["s"])) for r in rows]


async def _profile_vec(person_id: str) -> list[float] | None:
    row = await fetch_all("SELECT summary_embedding::text AS v FROM people WHERE id = %s AND summary_embedding IS NOT NULL",
                          (person_id,))
    return [float(x) for x in row[0]["v"].strip("[]").split(",")] if row else None


class HybridMatcher(MuseMatcher):
    async def _roster(self, shortlist: list[str]) -> str:
        return "\n\n".join((await expertise_cards(sorted(set(shortlist)))).values())

    async def _extra_for_person(self, person_id: str, pool: list[str], space: SkillSpace) -> str:
        vec = await _profile_vec(person_id)
        if vec is None:
            return ""
        found = await nearest_people(vec, {person_id, *pool}, settings.EMBED_POOL)
        return ("\n" + _candidate_lines(found, await _names(), "profile similarity")) if found else ""

    async def _extra_for_task(self, task_id: int, task: dict, available: set[str], pool: list[tuple[str, float]],
                              space: SkillSpace) -> list[tuple[str, float]]:
        [vec] = await embedder.embed([task["summary"]])
        await execute("UPDATE tasks SET embedding = %s::vector WHERE id = %s", (embedder.literal(vec), task_id))
        taken = {task["person_id"], *(p for p, _ in pool)}
        found = await nearest_work(vec, taken, settings.EMBED_POOL)
        return [(p, space.task_relevance(p, task["required_skills"])) for p, _ in found if p in available]

    async def _extra_for_query(self, query: str, pool: list[tuple[str, float]], space: SkillSpace,
                               required: list[dict]) -> str:
        [vec] = await embedder.embed([query])
        found = await nearest_work(vec, {p for p, _ in pool}, settings.EMBED_POOL)
        return ("\n" + _candidate_lines(found, await _names(), "recent work similarity")) if found else ""
