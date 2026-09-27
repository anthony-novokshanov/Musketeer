from app.api.auto_group import pick_team

REQ = [{"skill_id": "kafka", "weight": 1.0}, {"skill_id": "rate", "weight": 0.5}]
LEVELS = {("a", "kafka"): 0.9, ("b", "kafka"): 0.85, ("c", "rate"): 0.6, ("d", "kafka"): 0.1, ("d", "rate"): 0.1}
cover = lambda p, s: LEVELS.get((p, s), 0.0)


def test_covers_every_skill_before_doubling_up():
    # b is the second-best kafka person, but c is the only one who adds rate limiting.
    assert pick_team(cover, ["a", "b", "c", "d", "e"], REQ, 2) == ["a", "c"]


def test_fills_remaining_seats_by_relevance_and_skips_the_irrelevant():
    assert pick_team(cover, ["a", "b", "c", "d", "e"], REQ, 5) == ["a", "c", "b", "d"]


def test_no_required_skills_means_no_team():
    assert pick_team(cover, ["a", "b"], [], 3) == []
