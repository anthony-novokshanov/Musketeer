import pytest

from app.matching.base import Match
from app.matching.scoring import adjust_task_candidate, finalize_reason, pair_score, symmetrize


def test_symmetrize_both_sides_and_one_side():
    pairs = symmetrize({
        "p_001": [Match(person_id="p_002", score=0.9, reason="A says"), Match(person_id="p_003", score=0.6, reason="x")],
        "p_002": [Match(person_id="p_003", score=0.5, reason="y"), Match(person_id="p_001", score=0.7, reason="B says")],
    })
    ab = pairs[("p_001", "p_002")]
    assert ab["semantic_score"] == pytest.approx(0.8)
    assert (ab["rank_a"], ab["rank_b"]) == (1, 2)
    assert ab["reason"] == "A says"
    ac = pairs[("p_001", "p_003")]
    assert (ac["semantic_score"], ac["rank_a"], ac["rank_b"]) == (0.6, 2, None)


def test_pair_score_with_and_without_dirs():
    overlap, score, shared = pair_score(0.8, {"a", "b"}, {"b", "c", "d"})
    assert overlap == pytest.approx(0.25)
    assert score == pytest.approx(0.8 + 0.15 * 0.5)
    assert shared == ["b"]
    assert pair_score(0.8, set(), {"b"}) == (0.0, 0.8, [])
    assert pair_score(0.95, {"a"}, {"a"})[1] == 1.0  # capped


def test_finalize_reason():
    assert finalize_reason("Both fixed lag.", ["shared/kafka-client"]) == "Both fixed lag. Both changed shared/kafka-client."
    assert finalize_reason("Both edited shared/kafka-client.", ["shared/kafka-client"]) == "Both edited shared/kafka-client."
    assert finalize_reason("Same work.", []) == "Same work."


def test_adjust_task_candidate():
    assert adjust_task_candidate(0.7, interacted_before=False, helper_requests_last_7_days=0) == pytest.approx(0.8)
    assert adjust_task_candidate(0.7, interacted_before=True, helper_requests_last_7_days=1) == pytest.approx(0.65)
    assert adjust_task_candidate(0.7, interacted_before=True, helper_requests_last_7_days=10) == pytest.approx(0.55)


def test_prefer_new_off_removes_bonus():
    assert adjust_task_candidate(0.7, interacted_before=False, helper_requests_last_7_days=0, prefer_new=False) == pytest.approx(0.7)
