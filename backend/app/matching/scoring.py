"""Pair scoring (spec §9.3) and task-candidate adjustment (spec §9.4). Pure functions."""
from app.config import settings
from app.matching.base import Match


def blend(semantic_score: float, temporal_score: float, dir_bonus: float = 0.0) -> float:
    """Muse is the judge, the temporal graph is the evidence; neither dominates."""
    return min(1.0, settings.W_SEMANTIC * semantic_score + settings.W_TEMPORAL * temporal_score
               + settings.DIR_BONUS_WEIGHT * dir_bonus)


def symmetrize(ranked: dict[str, list[Match]]) -> dict[tuple[str, str], dict]:
    """Merge each person's ranked list into one entry per unordered pair (a < b).

    Both sides list the pair -> semantic_score is the mean; one side -> that score.
    rank_a/rank_b are filled in later by rank_lists, from final scores.
    Keeps the reason from the higher-scoring side.
    """
    sides: dict[tuple[str, str], dict[str, Match]] = {}
    for owner, matches in ranked.items():
        for m in matches:
            if m.person_id == owner:
                continue
            key = tuple(sorted((owner, m.person_id)))
            sides.setdefault(key, {})[owner] = m

    pairs = {}
    for (a, b), by_owner in sides.items():
        scores = [m.semantic_score for m in by_owner.values()]
        best = max(by_owner.values(), key=lambda m: m.semantic_score)
        pairs[(a, b)] = {
            "semantic_score": sum(scores) / len(scores),
            "listed_by_a": a in by_owner,
            "listed_by_b": b in by_owner,
            "reason": best.reason,
        }
    return pairs


def rank_lists(pairs: dict[tuple[str, str], dict]) -> None:
    """Set rank_a (b's rank in a's list) and rank_b from final scores, 1 = best. A side that didn't
    list the pair gets NULL. pairs values need score, listed_by_a, listed_by_b."""
    lists: dict[str, list[tuple[float, str, tuple[str, str]]]] = {}
    for (a, b), pr in pairs.items():
        pr["rank_a"] = pr["rank_b"] = None
        if pr["listed_by_a"]:
            lists.setdefault(a, []).append((pr["score"], b, (a, b)))
        if pr["listed_by_b"]:
            lists.setdefault(b, []).append((pr["score"], a, (a, b)))
    for owner, items in lists.items():
        for rank, (_, _, key) in enumerate(sorted(items, key=lambda x: (-x[0], x[1])), start=1):
            pairs[key]["rank_a" if key[0] == owner else "rank_b"] = rank


def pair_score(semantic_score: float, temporal_score: float, dirs_a: set[str], dirs_b: set[str]
               ) -> tuple[float, float, list[str]]:
    """Returns (dir_overlap, score, shared_dirs). Pass empty dir sets for non-engineers."""
    union = dirs_a | dirs_b
    shared = sorted(dirs_a & dirs_b)
    dir_overlap = len(shared) / len(union) if dirs_a and dirs_b else 0.0
    dir_bonus = min(1.0, 2 * dir_overlap)
    return dir_overlap, blend(semantic_score, temporal_score, dir_bonus), shared if dirs_a and dirs_b else []


def finalize_reason(reason: str, shared_dirs: list[str]) -> str:
    """Append the first shared directory unless the reason already names shared code."""
    if shared_dirs and not any(d in reason for d in shared_dirs):
        return f"{reason.rstrip()} Both changed {shared_dirs[0]}."
    return reason


def adjust_task_candidate(score: float, interacted_before: bool, helper_requests_last_7_days: int,
                          rated_unhelpful: bool = False) -> float:
    """Availability and novelty on top of relevance + recency (already in score)."""
    adjusted = score
    if not interacted_before:
        adjusted += settings.NOVELTY_BONUS
    adjusted -= min(settings.LOAD_PENALTY_MAX, settings.LOAD_PENALTY_PER_REQUEST * helper_requests_last_7_days)
    if rated_unhelpful:
        adjusted -= settings.UNHELPFUL_PENALTY
    return adjusted
