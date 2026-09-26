"""Pair scoring (spec §8.3) and task-candidate adjustment (spec §8.4). Pure functions."""
from app.matching.base import Match


def symmetrize(ranked: dict[str, list[Match]]) -> dict[tuple[str, str], dict]:
    """Merge each person's ranked list into one entry per unordered pair (a < b).

    Both sides list the pair -> semantic_score is the mean; one side -> that score.
    rank_a = rank of b in a's list, rank_b = rank of a in b's list (1 = best).
    Keeps the reason from the higher-scoring side.
    """
    sides: dict[tuple[str, str], dict[str, tuple[int, Match]]] = {}
    for owner, matches in ranked.items():
        for rank, m in enumerate(matches, start=1):
            if m.person_id == owner:
                continue
            key = tuple(sorted((owner, m.person_id)))
            sides.setdefault(key, {})[owner] = (rank, m)

    pairs = {}
    for (a, b), by_owner in sides.items():
        scores = [m.score for _, m in by_owner.values()]
        best = max(by_owner.values(), key=lambda rm: rm[1].score)[1]
        pairs[(a, b)] = {
            "semantic_score": sum(scores) / len(scores),
            "rank_a": by_owner[a][0] if a in by_owner else None,
            "rank_b": by_owner[b][0] if b in by_owner else None,
            "reason": best.reason,
        }
    return pairs


def pair_score(semantic_score: float, dirs_a: set[str], dirs_b: set[str]) -> tuple[float, float, list[str]]:
    """Returns (dir_overlap, score, shared_dirs). Pass empty dir sets for non-engineers."""
    union = dirs_a | dirs_b
    shared = sorted(dirs_a & dirs_b)
    dir_overlap = len(shared) / len(union) if dirs_a and dirs_b else 0.0
    dir_bonus = min(1.0, 2 * dir_overlap)
    return dir_overlap, min(1.0, semantic_score + 0.15 * dir_bonus), shared if dirs_a and dirs_b else []


def finalize_reason(reason: str, shared_dirs: list[str]) -> str:
    """Append the first shared directory unless the reason already names shared code."""
    if shared_dirs and not any(d in reason for d in shared_dirs):
        return f"{reason.rstrip()} Both changed {shared_dirs[0]}."
    return reason


def adjust_task_candidate(score: float, interacted_before: bool, helper_requests_last_7_days: int,
                          prefer_new: bool = True) -> float:
    adjusted = score
    if prefer_new and not interacted_before:
        adjusted += 0.10
    return adjusted - min(0.15, 0.05 * helper_requests_last_7_days)
