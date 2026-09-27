import itertools
from datetime import datetime, timedelta, timezone

from app.db import fetch_all, fetch_one
from scripts.run_pipeline import report


async def test_seed_load_counts(seeded):
    s = seeded["load_summary"]
    assert s["people"] == 40
    assert s["connections"] == 25
    n_events = (await fetch_one("SELECT count(*) AS n FROM activity_events"))["n"]
    assert n_events == s["events"] > 0
    teams = await fetch_all("SELECT lead_id FROM teams")
    assert len(teams) == 8 and all(t["lead_id"] for t in teams)


async def test_timestamps_shifted_to_now(seeded):
    latest = (await fetch_one("SELECT max(time) AS t FROM activity_events"))["t"]
    now = datetime.now(timezone.utc)
    assert now - timedelta(days=7) < latest <= now


async def test_seed_history_statuses_and_exclusions(seeded):
    rows = await fetch_all("SELECT status, count(*) AS n FROM connections WHERE is_seed GROUP BY status")
    assert {r["status"]: r["n"] for r in rows} == {"active": 18, "declined": 3, "expired": 2, "accepted": 2,
                                                         "requested": 3}   # requested: seed_spec pending_intros

    conns = await fetch_all("""SELECT c.requester_id, c.helper_id, ta.id AS ta, tb.id AS tb, ta.org_id AS oa, tb.org_id AS ob
                               FROM connections c JOIN people a ON a.id = c.requester_id JOIN people b ON b.id = c.helper_id
                               JOIN teams ta ON ta.id = a.team_id JOIN teams tb ON tb.id = b.team_id
                               WHERE c.status <> 'requested'""")   # history only, not the pending intros
    banned = {frozenset(p) for ids in seeded["planted"].values() for p in itertools.combinations(ids, 2)}
    assert not any(frozenset((c["requester_id"], c["helper_id"])) in banned for c in conns)
    assert not any({c["ta"], c["tb"]} == {"t_uni", "t_events"} for c in conns)
    assert sum(c["oa"] != c["ob"] for c in conns) == 3
    assert sum(c["ta"] != c["tb"] and c["oa"] == c["ob"] for c in conns) == 8

    # Every active connection has matching message events, and feedback events for both people.
    bad = await fetch_one("""
        SELECT count(*) AS n FROM connections c WHERE status = 'active' AND (
          c.message_count <> (SELECT count(*) FROM connection_events e WHERE e.connection_id = c.id AND e.event = 'message')
          OR (SELECT count(*) FROM connection_events e WHERE e.connection_id = c.id AND e.event = 'feedback') <> 2)""")
    assert bad["n"] == 0


async def test_amber_task(seeded):
    task = await fetch_one("SELECT person_id, status, created_at FROM tasks WHERE summary LIKE 'Build a candidate%'")
    assert task["person_id"] == seeded["planted"]["amber_case"][0]
    assert task["status"] == "open"
    assert timedelta(days=1.9) < datetime.now(timezone.utc) - task["created_at"] < timedelta(days=2.1)


async def test_trend_cagg_has_seed_history(seeded):
    rows = await fetch_all("SELECT event, sum(n) AS n FROM connection_daily GROUP BY event")
    by_event = {r["event"]: r["n"] for r in rows}
    assert by_event["suggested"] == 28 and by_event["accepted"] == 20


async def test_profiles_written(seeded):
    missing = await fetch_one("SELECT count(*) AS n FROM people WHERE summary IS NULL")
    assert missing["n"] == 0
    assert (await fetch_one("SELECT count(*) AS n FROM teams WHERE summary IS NULL"))["n"] == 0


async def test_similarities(seeded):
    kafka_a, kafka_b = sorted(seeded["planted"]["kafka_lag"])
    edge = await fetch_one("SELECT * FROM similarities WHERE person_a = %s AND person_b = %s", (kafka_a, kafka_b))
    assert edge["rank_a"] == 1 and edge["rank_b"] == 1
    assert edge["semantic_score"] == 0.95
    assert edge["shared_dirs"] == ["shared/kafka-client"]
    assert edge["score"] > edge["semantic_score"]  # code-overlap bonus
    assert "Both changed shared/kafka-client." in edge["reason"]
    assert (await fetch_one("SELECT count(*) AS n FROM similarities WHERE person_a >= person_b "
                            "OR 'p_999' IN (person_a, person_b)"))["n"] == 0


async def test_team_overlaps_and_brief(seeded):
    rows = await fetch_all("SELECT team_a, team_b, score, lead_brief FROM team_overlaps ORDER BY score DESC")
    assert (rows[0]["team_a"], rows[0]["team_b"], rows[0]["lead_brief"]) == ("t_events", "t_uni", "**Brief**")
    assert rows[1]["lead_brief"] is None  # below BRIDGE_MIN_SCORE: no brief


async def test_report_passes(seeded):
    assert await report(seeded["planted"]) == []
