"""Person skill vectors, temporal similarity, task relevance (spec §7.5).

    v_p[s]              = level(p, s)
    ṽ_p                 = v_p + RELATED_SKILL_WEIGHT · A v_p       A = skill-edge matrix, rows scaled to sum <= 1
    temporal_sim(a, b)  = cosine(ṽ_a, ṽ_b)                        in [0, 1]; 0 if either is empty
    shared_skills(a, b) = skills where both levels >= 0.3, by min(level_a, level_b) desc
    cover(p, s)         = max(level(p, s), RELATED_SKILL_WEIGHT · max_s' A[s, s'] · level(p, s'))
    task_relevance(p,R) = Σ w_s · cover(p, s) / Σ w_s

The whole graph is ~40 people x ~100 skills, so it is loaded into NumPy in one go.
"""
from dataclasses import dataclass

import numpy as np

from app.config import settings
from app.db import fetch_all

SHARED_MIN_LEVEL = 0.3


@dataclass
class SkillSpace:
    people: list[str]
    skills: list[str]
    names: dict[str, str]            # skill id -> name
    V: np.ndarray                    # people x skills, levels
    A: np.ndarray                    # skills x skills, row-scaled edge weights

    def __post_init__(self):
        self.p_idx = {p: i for i, p in enumerate(self.people)}
        self.s_idx = {s: i for i, s in enumerate(self.skills)}
        smoothed = self.V + settings.RELATED_SKILL_WEIGHT * self.V @ self.A.T
        norms = np.linalg.norm(smoothed, axis=1, keepdims=True)
        self._unit = np.divide(smoothed, norms, out=np.zeros_like(smoothed), where=norms > 0)

    def _row(self, person_id: str) -> int | None:
        return self.p_idx.get(person_id)

    def temporal_sim(self, a: str, b: str) -> float:
        ia, ib = self._row(a), self._row(b)
        if ia is None or ib is None:
            return 0.0
        return float(np.clip(self._unit[ia] @ self._unit[ib], 0.0, 1.0))

    def similar_people(self, person_id: str) -> list[tuple[str, float]]:
        """Everyone else by temporal_sim, best first (people with no skills omitted)."""
        i = self._row(person_id)
        if i is None:
            return []
        sims = self._unit @ self._unit[i]
        order = np.argsort(-sims, kind="stable")
        return [(self.people[j], float(sims[j])) for j in order if j != i and sims[j] > 0]

    def shared_skills(self, a: str, b: str) -> list[str]:
        ia, ib = self._row(a), self._row(b)
        if ia is None or ib is None:
            return []
        both = np.minimum(self.V[ia], self.V[ib])
        keep = [(both[k], self.skills[k]) for k in range(len(self.skills))
                if self.V[ia, k] >= SHARED_MIN_LEVEL and self.V[ib, k] >= SHARED_MIN_LEVEL]
        return [s for _, s in sorted(keep, key=lambda x: (-x[0], x[1]))]

    def level(self, person_id: str, skill_id: str) -> float:
        i, k = self._row(person_id), self.s_idx.get(skill_id)
        return 0.0 if i is None or k is None else float(self.V[i, k])

    def cover(self, person_id: str, skill_id: str) -> float:
        i, k = self._row(person_id), self.s_idx.get(skill_id)
        if i is None or k is None:
            return 0.0
        related = settings.RELATED_SKILL_WEIGHT * float(np.max(self.A[k] * self.V[i])) if len(self.skills) else 0.0
        return max(float(self.V[i, k]), related)

    def task_relevance(self, person_id: str, required: list[dict]) -> float:
        """required = [{"skill_id": ..., "weight": 0..1}] (tasks.required_skills)."""
        total = sum(r["weight"] for r in required)
        if total <= 0:
            return 0.0
        return sum(r["weight"] * self.cover(person_id, r["skill_id"]) for r in required) / total

    def rank_for_skills(self, required: list[dict], exclude: set[str] = frozenset()) -> list[tuple[str, float]]:
        scored = [(p, self.task_relevance(p, required)) for p in self.people if p not in exclude]
        return sorted([s for s in scored if s[1] > 0], key=lambda s: (-s[1], s[0]))


async def load() -> SkillSpace:
    people = [r["id"] for r in await fetch_all("SELECT id FROM people ORDER BY id")]
    skills = await fetch_all("SELECT id, name FROM skills ORDER BY id")
    ids = [s["id"] for s in skills]
    p_idx, s_idx = {p: i for i, p in enumerate(people)}, {s: i for i, s in enumerate(ids)}

    V = np.zeros((len(people), len(ids)))
    for r in await fetch_all("SELECT person_id, skill_id, level FROM person_skill_state"):
        if r["person_id"] in p_idx and r["skill_id"] in s_idx:
            V[p_idx[r["person_id"]], s_idx[r["skill_id"]]] = r["level"]

    A = np.zeros((len(ids), len(ids)))
    for e in await fetch_all("SELECT skill_a, skill_b, weight FROM skill_edges"):
        a, b = s_idx.get(e["skill_a"]), s_idx.get(e["skill_b"])
        if a is not None and b is not None:
            A[a, b] = A[b, a] = e["weight"]
    row_sums = A.sum(axis=1, keepdims=True)
    A = np.divide(A, np.maximum(row_sums, 1.0))
    return SkillSpace(people, ids, {s["id"]: s["name"] for s in skills}, V, A)
