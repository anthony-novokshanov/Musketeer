import httpx
import pytest

from app.db import execute, fetch_one
from app.main import app


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def edge(graph: dict, x: str, y: str) -> dict | None:
    a, b = sorted((x, y))
    return next((e for e in graph["edges"] if (e["a"], e["b"]) == (a, b)), None)


async def test_graph_shape_and_viewer(client, seeded):
    viewer = seeded["demo"]["viewer_id"]
    g = (await client.get("/api/graph", params={"viewer_id": viewer})).json()
    assert g["viewer"]["team_id"] == "t_events"
    assert len(g["orgs"]) == 3 and len(g["teams"]) == 8 and len(g["people"]) == 40
    reports = {p["id"] for p in g["people"] if p["is_viewer_report"]}
    assert seeded["planted"]["recruiting_mlh"][1] in reports and viewer not in reports
    assert sum(p["is_lead"] for p in g["people"]) == 8
    assert all(e["a"] < e["b"] for e in g["edges"])
    assert [(b["team_a"], b["team_b"], b["has_connection"]) for b in g["bridges"]] == [("t_events", "t_uni", False)]


async def test_graph_amber_person(client, seeded):
    amber = seeded["planted"]["amber_case"][0]
    g = (await client.get("/api/graph")).json()
    person = next(p for p in g["people"] if p["id"] == amber)
    assert person["needs_connection"] is True
    assert person["open_task"]["summary"].startswith("Build a candidate")
    assert sum(p["needs_connection"] for p in g["people"]) == 1


async def test_edge_states_follow_latest_connection(client, seeded):
    x, y = seeded["planted"]["recruiting_mlh"]
    assert edge((await client.get("/api/graph")).json(), x, y)["state"] == "potential"

    conn = await fetch_one("""INSERT INTO connections (requester_id, helper_id, origin, reason, status)
                              VALUES (%s, %s, 'auto', 'test', 'suggested') RETURNING id""", (y, x))
    try:
        e = edge((await client.get("/api/graph")).json(), x, y)
        assert (e["state"], e["connection_id"], e["rank"]) == ("pending", conn["id"], 1)
        await execute("UPDATE connections SET status = 'active', message_count = 5 WHERE id = %s", (conn["id"],))
        e = edge((await client.get("/api/graph")).json(), x, y)
        assert (e["state"], e["message_count"]) == ("connected", 5)
    finally:
        await execute("DELETE FROM connections WHERE id = %s", (conn["id"],))


async def test_connected_pair_without_similarity_is_an_edge(client, seeded):
    row = await fetch_one("""SELECT a.id AS x, b.id AS y FROM people a JOIN people b ON a.id < b.id
                             WHERE NOT EXISTS (SELECT 1 FROM similarities s WHERE s.person_a = a.id AND s.person_b = b.id)
                               AND NOT EXISTS (SELECT 1 FROM connections c WHERE a.id IN (c.requester_id, c.helper_id)
                                                                           AND b.id IN (c.requester_id, c.helper_id))
                             LIMIT 1""")
    conn = await fetch_one("""INSERT INTO connections (requester_id, helper_id, origin, reason, status, match_score)
                              VALUES (%s, %s, 'lead_intro', 'test', 'accepted', 0.5) RETURNING id""", (row["x"], row["y"]))
    try:
        e = edge((await client.get("/api/graph")).json(), row["x"], row["y"])
        assert (e["state"], e["rank"], e["score"]) == ("connected", None, 0.5)
    finally:
        await execute("DELETE FROM connections WHERE id = %s", (conn["id"],))


async def test_person_engineer_and_non_engineer(client, seeded):
    kafka = seeded["planted"]["kafka_lag"][0]
    p = (await client.get(f"/api/people/{kafka}")).json()
    assert "shared/kafka-client" in p["github"]["top_directories"]
    assert p["github"]["commit_mix"] == {"feat": 3, "fix": 3}
    assert p["matches"][0]["person_id"] == seeded["planted"]["kafka_lag"][1]
    assert p["matches"][0]["shared_dirs"] == ["shared/kafka-client"]
    scores = [m["score"] for m in p["matches"]]
    assert scores == sorted(scores, reverse=True) and len(scores) <= 8

    recruiter = (await client.get(f"/api/people/{seeded['planted']['recruiting_mlh'][0]}")).json()
    assert recruiter["github"] is None


async def test_person_connections_helpful_is_own_rating(client, seeded):
    c = await fetch_one("SELECT id, requester_id, helper_id, helpful_requester, helpful_helper FROM connections "
                        "WHERE status = 'active' AND helpful_requester IS DISTINCT FROM helpful_helper LIMIT 1")
    if c is None:
        pytest.skip("no active connection with differing ratings in this seed")
    for pid, col in ((c["requester_id"], "helpful_requester"), (c["helper_id"], "helpful_helper")):
        conns = (await client.get(f"/api/people/{pid}")).json()["connections"]
        assert next(x for x in conns if x["id"] == c["id"])["helpful"] == c[col]


async def test_pair(client, seeded):
    x, y = seeded["planted"]["kafka_lag"]
    p = (await client.get(f"/api/pairs/{y}/{x}")).json()
    assert p["a"]["id"] == y and p["semantic_score"] == 0.95 and p["shared_dirs"] == ["shared/kafka-client"]
    assert p["connections"] == []

    c = await fetch_one("SELECT requester_id, helper_id FROM connections WHERE status = 'active' LIMIT 1")
    p = (await client.get(f"/api/pairs/{c['requester_id']}/{c['helper_id']}")).json()
    timeline = [e["event"] for e in p["connections"][0]["timeline"]]
    assert timeline[:3] == ["suggested", "requested", "accepted"] and "message" not in timeline
    assert p["connections"][0]["task_summary"] == "Earlier task (seed history)"


async def test_bridge(client, seeded):
    b = (await client.get("/api/bridges/t_uni/t_events")).json()
    assert b["team_a"]["id"] == "t_uni" and b["team_a"]["lead"]["id"]
    assert b["score"] == 0.88 and b["lead_brief"] == "**Brief**" and b["connections_count"] == 0
    x, y = sorted(seeded["planted"]["recruiting_mlh"])
    assert (b["top_pairs"][0]["a"], b["top_pairs"][0]["b"]) == (x, y)


async def test_errors_are_json(client, seeded):
    r = await client.get("/api/people/p_nope")
    assert r.status_code == 404 and r.json() == {"error": "person p_nope not found"}
    assert (await client.get("/api/bridges/t_ads/t_design")).status_code == 404
    r = await client.get("/api/search")
    assert r.status_code == 422 and "q" in r.json()["error"]


async def test_search(client, seeded, fake_muse):
    target = seeded["planted"]["rate_limit"][0]
    fake_muse.handler = lambda prompt, schema: {"results": [
        {"person_id": target, "score": 93, "reason": "Built request throttling."},
        {"person_id": "p_999", "score": 90, "reason": "hallucinated"}]}
    r = (await client.get("/api/search", params={"q": "who has built rate limiting?"})).json()
    assert r["results"] == [{"person_id": target, "name": f"Person {target}", "team_id": "t_ads",
                             "team_name": "Ads Ranking", "org_id": "org_eng", "score": 0.93,
                             "reason": "Built request throttling."}]
    assert "who has built rate limiting?" in fake_muse.calls[0][1][-1]["content"]


async def test_synopsis(client, seeded, fake_muse):
    fake_muse.handler = lambda prompt, schema: {"summary": "20 connections this month."}
    s = (await client.get("/api/synopsis", params={"days": 30})).json()
    assert s["summary"] == "20 connections this month."
    expect = await fetch_one("""
        SELECT count(*) FILTER (WHERE accepted_at > now() - INTERVAL '30 days') AS accepted,
               count(*) FILTER (WHERE created_at >= date_trunc('day', now()) - INTERVAL '29 days') AS suggested,
               count(*) FILTER (WHERE accepted_at >= date_trunc('day', now()) - INTERVAL '29 days') AS accepted_trend
        FROM connections""")
    assert 0 < s["stats"]["connections_made"] == expect["accepted"] <= 20
    assert 0 <= s["stats"]["helpful_rate"] <= 1 and 0 <= s["stats"]["cross_team_share"] <= 1
    assert s["stats"]["median_minutes_to_connect"] > 0
    assert len(s["trend"]) == 30
    assert sum(t["suggested"] for t in s["trend"]) == expect["suggested"]
    assert sum(t["accepted"] for t in s["trend"]) == expect["accepted_trend"]
    assert [(t["team_a"], t["team_b"]) for t in s["teams_should_talk"]] == [("t_events", "t_uni")]
    planted = {tuple(sorted(p)) for k, p in seeded["planted"].items()}
    assert planted <= {(m["a"], m["b"]) for m in s["missed_opportunities"]}
