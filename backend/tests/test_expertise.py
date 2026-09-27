"""Temporal expertise graph (spec §7): extraction, canonicalization, decay, skill graph, vectors, feedback."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.connectors.slack_ingest import ingest_channel_message
from app.db import execute, fetch_all, fetch_one
from app.expertise import decay, feedback, vectors
from app.expertise.canonicalize import skill_id
from app.expertise.extract import WEIGHTS, extract_all, ingest_did
from app.schemas import NormalizedEvent


def test_skill_id_is_deterministic_slug():
    assert skill_id("Kafka consumer lag") == "sk_kafka_consumer_lag"
    assert skill_id("CI/CD  pipelines!") == "sk_ci_cd_pipelines"


async def test_batch_extraction_merges_planted_wording(seeded):
    assert {r["id"] for r in await fetch_all("SELECT id FROM skills")} == {
        "sk_kafka_consumer_lag", "sk_service_maintenance", "sk_hackathon_booths", "sk_rate_limiting"}
    labels = {r["raw_label"]: r["skill_id"] for r in await fetch_all("SELECT * FROM skill_labels")}
    assert labels["consumer lag"] == labels["notifications backlog"] == "sk_kafka_consumer_lag"
    assert labels["service maintenance"] == "sk_service_maintenance"
    for pid in seeded["planted"]["kafka_lag"]:
        assert await fetch_one("SELECT 1 FROM expertise_events WHERE person_id = %s AND skill_id = 'sk_kafka_consumer_lag'",
                               (pid,))
    # P13's related edge survives the skill-graph rebuild; a related name Muse didn't define is ignored.
    edge = await fetch_one("""SELECT weight, origin FROM skill_edges
                              WHERE skill_a = 'sk_kafka_consumer_lag' AND skill_b = 'sk_service_maintenance'""")
    assert edge["origin"] in ("muse", "both") and edge["weight"] >= 0.5


async def test_evidence_rows(seeded):
    assert (await fetch_one("SELECT count(*) AS n FROM expertise_events WHERE confidence < 0.6"))["n"] == 0
    kinds = await fetch_all("SELECT DISTINCT evidence_kind, weight FROM expertise_events")
    kinds = [r for r in kinds if r["evidence_kind"] in WEIGHTS]
    assert {r["evidence_kind"]: r["weight"] for r in kinds} == {k: WEIGHTS[k] for k in ("solved", "built", "reviewed", "organized")}
    # Evidence time is the time of the work, and every activity row points back at its event.
    bad = await fetch_one("""SELECT count(*) AS n FROM expertise_events x LEFT JOIN activity_events e
                             ON e.id = x.source_event_id AND e.time = x.time
                             WHERE x.source = 'activity' AND (e.id IS NULL OR e.person_id <> x.person_id)""")
    assert bad["n"] == 0
    flags = await fetch_all("SELECT direction, expertise_extracted, count(*) AS n FROM activity_events GROUP BY 1, 2")
    assert {(r["direction"], r["expertise_extracted"]) for r in flags} <= {("did", True), ("received", False)}


async def test_rerun_writes_nothing(seeded):
    before = (await fetch_one("SELECT count(*) AS n FROM expertise_events"))["n"]
    assert await extract_all() == 0
    assert (await fetch_one("SELECT count(*) AS n FROM expertise_events"))["n"] == before


@pytest.fixture
async def client():
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def live_handler(prompt, schema):
    name = schema.__name__
    if name == "ExtractExpertiseOut":
        return {"items": [{"label": "Kafka backlog", "evidence_kind": "solved", "confidence": 0.95,
                           "snippet": "drained a stuck queue " + "x" * 200},
                          {"label": "booth logistics", "evidence_kind": "organized", "confidence": 0.8,
                           "snippet": "ran a hackathon booth"}]}
    if name == "CanonicalizeLabelOut":
        if "Kafka backlog".lower() in prompt:
            assert "kafka consumer lag: Queues falling behind." in prompt   # sees the current vocabulary
            return {"skill_name": "kafka consumer lag", "is_new": False}
        return {"skill_name": "Hackathon Booth Logistics", "is_new": True, "kind": "domain",
                "description": "Running booths at hackathons."}
    raise AssertionError(name)


def did_event(person_id: str, external_id: str) -> NormalizedEvent:
    return NormalizedEvent(source="slack", kind="slack_message", external_id=external_id,
                           time=datetime.now(timezone.utc), person_id=person_id, direction="did", title=None,
                           text="Fixed the stuck queue and ran the booth.", metadata={})


async def test_live_event_canonicalizes_incrementally(seeded, fake_muse):
    fake_muse.handler = live_handler
    person = seeded["planted"]["recruiting_mlh"][0]
    try:
        evidence = await ingest_did(did_event(person, "live-1"))
        assert {(e["skill_id"], e["evidence_kind"]) for e in evidence} == {
            ("sk_kafka_consumer_lag", "solved"), ("sk_hackathon_booth_logistics", "organized")}
        assert all(len(e["snippet"]) <= 120 for e in evidence)
        assert (await fetch_one("SELECT kind FROM skills WHERE id = 'sk_hackathon_booth_logistics'"))["kind"] == "domain"

        # Same labels again: mapped from skill_labels, no P13 call. Duplicate event: nothing at all.
        fake_muse.calls.clear()
        second = did_event(person, "live-2")
        assert len(await ingest_did(second)) == 2
        assert "CanonicalizeLabelOut" not in [c[0] for c in fake_muse.calls]
        assert await ingest_did(second) is None
    finally:
        await _forget_live()


async def test_public_slack_post_is_evidence_for_mapped_sender(seeded, fake_muse):
    fake_muse.handler = live_handler
    person = seeded["planted"]["recruiting_mlh"][0]
    await execute("UPDATE people SET slack_user_id = 'USENDER' WHERE id = %s", (person,))
    try:
        assert await ingest_channel_message({"channel": "C9", "ts": "1790000000.000200", "user": "USENDER",
                                             "text": "fixed the stuck queue, thanks <@UNOBODY>"}) == []
        ev = await fetch_one("SELECT direction, text, expertise_extracted FROM activity_events WHERE external_id = %s",
                             ("C9:1790000000.000200",))
        assert (ev["direction"], ev["expertise_extracted"]) == ("did", True)
        assert "<@" not in ev["text"]
        # Unmapped sender: not ingested.
        await ingest_channel_message({"channel": "C9", "ts": "1790000000.000300", "user": "UOTHER", "text": "hello"})
        assert not await fetch_one("SELECT 1 FROM activity_events WHERE external_id = 'C9:1790000000.000300'")
    finally:
        await execute("UPDATE people SET slack_user_id = NULL WHERE id = %s", (person,))
        await _forget_live()


async def _forget_live():
    """Undo live-test rows so other tests in this module see the batch state."""
    await execute("DELETE FROM person_skill_state WHERE skill_id = 'sk_hackathon_booth_logistics'")
    await execute("""DELETE FROM expertise_events WHERE source_event_id IN
                     (SELECT id FROM activity_events WHERE external_id LIKE 'live-%%' OR external_id LIKE 'C9:%%')""")
    await execute("DELETE FROM activity_events WHERE external_id LIKE 'live-%%' OR external_id LIKE 'C9:%%'")
    await execute("DELETE FROM skill_labels WHERE raw_label IN ('kafka backlog', 'booth logistics')")
    await execute("DELETE FROM skills WHERE id = 'sk_hackathon_booth_logistics'")
    await decay.recompute_all()


# --- decay (§7.3) ---

def test_decay_formula_matches_spec_examples():
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    fix = lambda days: (now - timedelta(days=days), "solved", 1.2, 1.0)
    assert decay.level(decay.strength([fix(1)], now)) == pytest.approx(0.45, abs=0.02)
    assert decay.level(decay.strength([fix(60)], now)) == pytest.approx(0.13, abs=0.02)
    assert decay.level(decay.strength([fix(d) for d in (1, 1, 2, 2, 3)], now)) == pytest.approx(0.95, abs=0.02)
    assert decay.strength([fix(200)], now) == 0   # outside EXPERTISE_WINDOW_DAYS
    helped = decay.strength([(now - timedelta(days=60), "helped", 1.0, 1.0)], now)
    assert helped == pytest.approx(0.5)            # 60-day half-life for feedback evidence


def test_trend_rules():
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    old = now - timedelta(days=90)
    assert decay.trend(1.0, 0.0, now - timedelta(days=3), now) == "new"
    assert decay.trend(1.3, 1.0, old, now) == "rising"
    assert decay.trend(0.5, 1.0, old, now) == "fading"
    assert decay.trend(1.0, 1.0, old, now) == "steady"


async def test_skill_state_sql_matches_python(seeded):
    person = seeded["planted"]["kafka_lag"][0]
    rows = await decay.state(person)
    assert rows and all(r["level"] >= 0.05 for r in rows)
    row = next(r for r in rows if r["skill_id"] == "sk_kafka_consumer_lag")
    ev = await fetch_all("""SELECT time, evidence_kind, weight, confidence FROM expertise_events
                            WHERE person_id = %s AND skill_id = 'sk_kafka_consumer_lag'""", (person,))
    expected = decay.strength([(e["time"], e["evidence_kind"], e["weight"], e["confidence"]) for e in ev],
                              row["computed_at"])
    assert row["strength"] == pytest.approx(expected, rel=1e-4)
    assert row["level"] == pytest.approx(decay.level(expected), rel=1e-4)
    focus = (await fetch_one("SELECT focus_areas FROM people WHERE id = %s", (person,)))["focus_areas"]
    assert focus[0] == rows[0]["name"] and len(focus) <= 6


# --- skill graph + vectors (§7.4, §7.5) ---

async def test_vectors(seeded):
    space = await vectors.load()
    kafka_a, kafka_b = seeded["planted"]["kafka_lag"]
    assert 0 < space.temporal_sim(kafka_a, kafka_b) <= 1
    assert space.temporal_sim(kafka_a, kafka_b) == pytest.approx(space.temporal_sim(kafka_b, kafka_a))
    assert "sk_kafka_consumer_lag" in space.shared_skills(kafka_a, kafka_b)
    # A related skill partly covers a requirement the person has no direct evidence for.
    uni = seeded["planted"]["recruiting_mlh"][0]
    assert space.task_relevance(uni, [{"skill_id": "sk_hackathon_booths", "weight": 1.0}]) > 0.5
    assert space.task_relevance(uni, [{"skill_id": "sk_rate_limiting", "weight": 1.0}]) == 0
    only_maint = next(p for p in space.people if space.level(p, "sk_service_maintenance") > 0
                      and space.level(p, "sk_kafka_consumer_lag") == 0)
    assert 0 < space.cover(only_maint, "sk_kafka_consumer_lag") < space.level(only_maint, "sk_service_maintenance")


async def test_similarities_carry_temporal_scores(seeded):
    a, b = sorted(seeded["planted"]["kafka_lag"])
    row = await fetch_one("SELECT * FROM similarities WHERE person_a = %s AND person_b = %s", (a, b))
    assert row["temporal_score"] > 0 and "sk_kafka_consumer_lag" in row["shared_skills"]
    task = await fetch_one("SELECT required_skills FROM tasks WHERE summary LIKE 'Build a candidate%'")
    assert task["required_skills"] == [{"skill_id": "sk_service_maintenance", "weight": 1.0}]


async def test_refresh_moves_edges_without_muse(seeded, fake_muse):
    from app.pipeline.similarity import refresh_temporal
    fake_muse.handler = lambda prompt, schema: (_ for _ in ()).throw(AssertionError("no Muse in refresh"))
    # Strong new evidence on a skill with no related edges pulls the pair apart.
    a, b = sorted(seeded["planted"]["rate_limit"])
    sql = "SELECT temporal_score, score FROM similarities WHERE person_a = %s AND person_b = %s"
    await refresh_temporal()                       # baseline from the current state
    before = await fetch_one(sql, (a, b))
    now = datetime.now(timezone.utc)
    await execute("""INSERT INTO expertise_events (time, person_id, skill_id, evidence_kind, weight, confidence,
                                                   source, snippet)
                     SELECT %s - make_interval(days => g), %s, 'sk_hackathon_booths', 'solved', 1.2, 1.0,
                            'feedback', 'refresh test' FROM generate_series(0, 4) g""", (now, a))
    try:
        await refresh_temporal()
        after = await fetch_one(sql, (a, b))
        assert after["temporal_score"] < before["temporal_score"] and after["score"] < before["score"]
        assert fake_muse.calls == []
    finally:
        await execute("DELETE FROM expertise_events WHERE snippet = 'refresh test'")
        await refresh_temporal()


# --- feedback (§7.6) ---

async def test_seed_feedback_applied(seeded):
    assert (await fetch_one("SELECT count(*) AS n FROM connections WHERE is_seed AND status = 'active' "
                            "AND NOT feedback_applied"))["n"] == 0
    n_ratings = (await fetch_one("""SELECT count(*) AS n FROM connection_events e JOIN connections c
                                    ON c.id = e.connection_id WHERE c.is_seed AND e.event = 'feedback'"""))["n"]
    assert (await fetch_one("SELECT count(*) AS n FROM match_feedback"))["n"] == n_ratings
    bad = await fetch_one("""SELECT count(*) AS n FROM expertise_events x JOIN connections c ON c.id = x.connection_id
                             WHERE x.source = 'seed_feedback' AND (NOT c.helpful_requester OR x.time > now())""")
    assert bad["n"] == 0


async def test_live_rating_writes_evidence_once(seeded, client):
    from app.bot import flows
    a, b = seeded["planted"]["kafka_lag"]
    conn = await fetch_one("""INSERT INTO connections (requester_id, helper_id, origin, reason, status)
                              VALUES (%s, %s, 'manager_nudge', 'test', 'active') RETURNING id""", (b, a))
    try:
        await flows.record_feedback(conn["id"], a, True)          # helper rates first: label only
        assert not await fetch_one("SELECT 1 FROM expertise_events WHERE connection_id = %s", (conn["id"],))
        await flows.record_feedback(conn["id"], b, True)          # requester: evidence for both
        rows = await fetch_all("SELECT person_id, skill_id, evidence_kind, weight FROM expertise_events "
                               "WHERE connection_id = %s AND source = 'feedback'", (conn["id"],))
        assert ("helped" in {r["evidence_kind"] for r in rows if r["person_id"] == a}
                and "learned" in {r["evidence_kind"] for r in rows if r["person_id"] == b})
        assert "sk_kafka_consumer_lag" in {r["skill_id"] for r in rows}
        assert await feedback.apply(conn["id"]) == 0 and await feedback.apply_pending() == 0   # once
        assert (await fetch_one("SELECT count(*) AS n FROM match_feedback WHERE connection_id = %s",
                                (conn["id"],)))["n"] == 2
        state = await client.get(f"/api/dev/expertise/{a}")
        assert any(e["evidence_kind"] == "helped" for e in state.json()["evidence"])
        assert (await client.post("/api/dev/reset-demo")).status_code == 200
        assert not await fetch_one("SELECT 1 FROM expertise_events WHERE source = 'feedback'")
        assert not await fetch_one("SELECT 1 FROM match_feedback WHERE connection_id = %s", (conn["id"],))
    finally:
        await execute("DELETE FROM connections WHERE id = %s", (conn["id"],))
