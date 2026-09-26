from typing import Protocol

from pydantic import BaseModel

from app.config import settings


class Match(BaseModel):
    person_id: str
    score: float        # 0..1, always calibrated by Muse (never a raw cosine)
    reason: str


class Matcher(Protocol):
    async def rank_for_person(self, person_id: str) -> list[Match]: ...   # up to TOPK_STORE
    async def rank_for_task(self, task_id: int) -> list[Match]: ...       # up to 3
    async def search(self, query: str) -> list[Match]: ...                # up to 5


def get_matcher() -> Matcher:
    if settings.MATCHER_MODE == "hybrid":
        from app.matching.hybrid_matcher import HybridMatcher
        return HybridMatcher()
    from app.matching.muse_matcher import MuseMatcher
    return MuseMatcher()
