import pytest

from app.matching.base import Match
from app.matching.scoring import adjust_task_candidate, blend, finalize_reason, pair_score, rank_lists, symmetrize


def m(pid: str, semantic: float, reason: str = "x") -> Match:
    return Match(person_id=pid, score=semantic, semantic_score=semantic, temporal_score=0.0, reason=reason)


def test_symmetrize_both_sides_and_one_side():
    pairs = symmetrize({
        "p_001": [m("p_002", 0.9, "A says"), m("p_003", 0.6)],
        "p_002": [m("p_003", 0.5), m("p_001", 0.7, "B says")],
    })
    ab = pairs[("p_001", "p_002")]
    assert ab["semantic_score"] == pytest.approx(0.8)
    assert (ab["listed_by_a"], ab["listed_by_b"], ab["reason"]) == (True, True, "A says")
    ac = pairs[("p_001", "p_003")]
    assert (ac["semantic_score"], ac["listed_by_a"], ac["listed_by_b"]) == (0.6, True, False)


def test_rank_lists_uses_final_scores():
    pairs = {("p_001", "p_002"): {"score": 0.5, "listed_by_a": True, "listed_by_b": True},
             ("p_001", "p_003"): {"score": 0.9, "listed_by_a": True, "listed_by_b": False}}
    rank_lists(pairs)
    assert (pairs[("p_001", "p_003")]["rank_a"], pairs[("p_001", "p_003")]["rank_b"]) == (1, None)
    assert (pairs[("p_001", "p_002")]["rank_a"], pairs[("p_001", "p_002")]["rank_b"]) == (2, 1)


def test_blend_weights():
    assert blend(0.9, 0.5) == pytest.approx(0.6 * 0.9 + 0.4 * 0.5)
    # Same Muse judgment, stale shared work scores lower than recent shared work.
    assert blend(0.9, 0.1) < blend(0.9, 0.8)
    assert blend(1.0, 1.0, 1.0) == 1.0   # capped


def test_pair_score_with_and_without_dirs():
    overlap, score, shared = pair_score(0.8, 0.5, {"a", "b"}, {"b", "c", "d"})
    assert overlap == pytest.approx(0.25)
    assert score == pytest.approx(0.6 * 0.8 + 0.4 * 0.5 + 0.15 * 0.5)
    assert shared == ["b"]
    assert pair_score(0.8, 0.5, set(), {"b"}) == (0.0, pytest.approx(0.68), [])


def test_finalize_reason():
    assert finalize_reason("Both fixed lag.", ["shared/kafka-client"]) == "Both fixed lag. Both changed shared/kafka-client."
    assert finalize_reason("Both edited shared/kafka-client.", ["shared/kafka-client"]) == "Both edited shared/kafka-client."
    assert finalize_reason("Same work.", []) == "Same work."


def test_adjust_task_candidate():
    assert adjust_task_candidate(0.7, interacted_before=False, helper_requests_last_7_days=0) == pytest.approx(0.8)
    assert adjust_task_candidate(0.7, interacted_before=True, helper_requests_last_7_days=1) == pytest.approx(0.65)
    assert adjust_task_candidate(0.7, interacted_before=True, helper_requests_last_7_days=10) == pytest.approx(0.55)
    assert adjust_task_candidate(0.7, True, 0, rated_unhelpful=True) == pytest.approx(0.6)


def test_prefer_new_off_removes_bonus():
    assert adjust_task_candidate(0.7, interacted_before=False, helper_requests_last_7_days=0, prefer_new=False) == pytest.approx(0.7)
