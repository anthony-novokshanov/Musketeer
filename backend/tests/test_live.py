import asyncio
import base64

import httpx
import pytest

from app.bot import flows, messages
from app.config import settings
from app.connectors import gmail
from app.connectors.slack_ingest import ingest_channel_message
from app.db import execute, fetch_all, fetch_one
from app.main import app


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def instant_simulation(monkeypatch, seeded, client):
    # These tests are about the Slack flows; the Muse score alone decides (blend covered in test_scoring).
    monkeypatch.setattr(settings, "W_SEMANTIC", 1.0)
    monkeypatch.setattr(settings, "W_TEMPORAL", 0.0)
    monkeypatch.setattr(settings, "SIMULATED_ACCEPT_SEC", 0)
    monkeypatch.setattr(flows, "SIMULATED_ACTIVE_AFTER_SEC", 0)
    yield
    assert (await client.post("/api/dev/reset-demo")).status_code == 200


def muse_for(candidates: list[tuple[str, int]], is_task: bool = True):
    def handler(prompt, schema):
        if schema.__name__ == "DetectTaskOut":
            return {"is_new_task": is_task, "confidence": 0.9, "summary": "Run the MLH booth", "task_type": "event"}
        if schema.__name__ == "RankForTaskOut":
            return {"candidates": [{"person_id": p, "score": s, "reason": f"{p} ran MLH booths."} for p, s in candidates]}
        raise AssertionError(schema.__name__)
    return handler


async def wait_for(sql: str, params: tuple, expected, timeout: float = 5.0):
    for _ in range(int(timeout / 0.05)):
        row = await fetch_one(sql, params)
        if row and row["v"] == expected:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {expected!r}; last {row!r}")


async def simulate(client, person_id: str) -> dict:
    r = await client.post("/api/dev/simulate-event", json={
        "person_id": person_id, "title": "MLH booth", "text": "Can you run our booth at the MLH hackathon next month?"})
    assert r.status_code == 200, r.text
    return r.json()


async def test_email_to_connected_end_to_end(client, seeded, fake_muse):
    priya, jordan = seeded["planted"]["recruiting_mlh"]
    fake_muse.handler = muse_for([(priya, 85)])
    res = await simulate(client, jordan)

    assert res["detection"]["is_new_task"] and res["task_id"] and res["connection_id"]
    assert res["candidates"][0]["person_id"] == priya
    assert res["candidates"][0]["adjusted"] == pytest.approx(0.95)  # +0.10 never interacted
    await wait_for("SELECT status AS v FROM connections WHERE id = %s", (res["connection_id"],), "active")

    conn = await fetch_one("SELECT * FROM connections WHERE id = %s", (res["connection_id"],))
    assert (conn["requester_id"], conn["helper_id"], conn["origin"], conn["message_count"]) == (jordan, priya, "auto", 4)
    task = await fetch_one("SELECT status, best_match_score FROM tasks WHERE id = %s", (res["task_id"],))
    assert task["status"] == "connected" and task["best_match_score"] == pytest.approx(0.95)
    events = [r["event"] for r in await fetch_all(
        "SELECT event FROM connection_events WHERE connection_id = %s ORDER BY time, event", (conn["id"],))]
    assert [e for e in events if e != "message"] == ["suggested", "requested", "accepted", "active"]

    g = (await client.get("/api/graph")).json()
    a, b = sorted((priya, jordan))
    assert next(e for e in g["edges"] if (e["a"], e["b"]) == (a, b))["state"] == "connected"


async def test_duplicate_event_is_ignored(client, seeded, fake_muse):
    from app.pipeline.tasks import ingest_received
    from app.schemas import NormalizedEvent
    fake_muse.handler = muse_for([], is_task=False)
    ev = NormalizedEvent(source="email", kind="email", external_id="dup-1", time="2026-09-20T00:00:00Z",
                         person_id="p_001", direction="received", title="x", text="y", metadata={})
    assert await ingest_received(ev) is not None
    assert await ingest_received(ev) is None


async def test_not_a_task(client, seeded, fake_muse):
    fake_muse.handler = muse_for([], is_task=False)
    res = await simulate(client, seeded["planted"]["recruiting_mlh"][1])
    assert res["task_id"] is None and res["connection_id"] is None


async def test_weak_match_leaves_task_open(client, seeded, fake_muse):
    priya, jordan = seeded["planted"]["recruiting_mlh"]
    fake_muse.handler = muse_for([(priya, 50)])
    res = await simulate(client, jordan)
    assert res["task_id"] and res["connection_id"] is None
    task = await fetch_one("SELECT status, best_match_score FROM tasks WHERE id = %s", (res["task_id"],))
    assert task["status"] == "open" and task["best_match_score"] == pytest.approx(0.60)


async def test_requester_not_now_dismisses_task(client, seeded, fake_muse, monkeypatch):
    monkeypatch.setattr(settings, "SIMULATED_ACCEPT_SEC", 999)  # nobody auto-clicks
    priya, jordan = seeded["planted"]["recruiting_mlh"]
    fake_muse.handler = muse_for([(priya, 85)])
    res = await simulate(client, jordan)
    await flows.requester_declines(res["connection_id"])
    await flows.requester_accepts(res["connection_id"])  # late click: ignored
    assert (await fetch_one("SELECT status FROM connections WHERE id = %s", (res["connection_id"],)))["status"] == "declined"
    assert (await fetch_one("SELECT status FROM tasks WHERE id = %s", (res["task_id"],)))["status"] == "dismissed"
    flows.cancel_background()


async def test_helper_declines_tries_next_candidate_once(client, seeded, fake_muse, monkeypatch):
    monkeypatch.setattr(settings, "SIMULATED_ACCEPT_SEC", 999)
    priya, jordan = seeded["planted"]["recruiting_mlh"]
    # People with no connection history, so no interaction bonus/penalty reorders them.
    fresh = await fetch_all("""SELECT id FROM people p WHERE id NOT IN (%(j)s, %(p)s) AND NOT EXISTS (
                                 SELECT 1 FROM connections c WHERE p.id IN (c.requester_id, c.helper_id))
                               ORDER BY id LIMIT 2""", {"j": jordan, "p": priya})
    second, third = (r["id"] for r in fresh)
    fake_muse.handler = muse_for([(priya, 85), (second, 80), (third, 78)])
    res = await simulate(client, jordan)

    await flows.requester_accepts(res["connection_id"])
    await flows.helper_declines(res["connection_id"])
    nxt = await fetch_one("SELECT id, helper_id, status FROM connections WHERE task_id = %s AND id <> %s",
                          (res["task_id"], res["connection_id"]))
    assert (nxt["helper_id"], nxt["status"]) == (second, "suggested")

    await flows.requester_accepts(nxt["id"])
    await flows.helper_declines(nxt["id"])  # second decline: no third try
    assert (await fetch_one("SELECT count(*) AS v FROM connections WHERE task_id = %s", (res["task_id"],)))["v"] == 2
    assert (await fetch_one("SELECT status FROM tasks WHERE id = %s", (res["task_id"],)))["status"] == "open"
    flows.cancel_background()


async def test_manager_nudge_clears_amber_and_reset_restores_it(client, seeded):
    sam, growth_pm = seeded["planted"]["amber_case"]
    viewer = seeded["demo"]["viewer_id"]
    r = await client.post("/api/actions/nudge", json={"viewer_id": viewer, "employee_id": sam, "target_id": growth_pm})
    assert r.status_code == 200 and r.json()["delivered"] == "simulated"
    dup = await client.post("/api/actions/nudge", json={"viewer_id": viewer, "employee_id": growth_pm, "target_id": sam})
    assert dup.status_code == 409

    await wait_for("SELECT status AS v FROM connections WHERE id = %s", (r.json()["connection_id"],), "active")
    conn = await fetch_one("SELECT origin, initiated_by, task_id FROM connections WHERE id = %s", (r.json()["connection_id"],))
    assert (conn["origin"], conn["initiated_by"]) == ("manager_nudge", viewer) and conn["task_id"]
    needs = lambda g: next(p for p in g["people"] if p["id"] == sam)["needs_connection"]
    assert needs((await client.get("/api/graph")).json()) is False

    assert (await client.post("/api/dev/reset-demo")).json()["amber_tasks_restored"] == 1
    assert needs((await client.get("/api/graph")).json()) is True


async def test_introduce_leads(client, seeded):
    r = await client.post("/api/actions/introduce-leads", json={"team_a": "t_events", "team_b": "t_uni"})
    assert r.status_code == 200
    conn = await fetch_one("""SELECT c.origin, r.team_id AS rt, h.team_id AS ht FROM connections c
                              JOIN people r ON r.id = c.requester_id JOIN people h ON h.id = c.helper_id
                              WHERE c.id = %s""", (r.json()["connection_id"],))
    assert (conn["origin"], conn["rt"], conn["ht"]) == ("lead_intro", "t_events", "t_uni")
    assert (await client.post("/api/actions/introduce-leads", json={"team_a": "t_ads", "team_b": "t_design"})).status_code == 404


async def test_group_dm_message_counting(seeded):
    x, y = seeded["planted"]["kafka_lag"]
    await execute("UPDATE people SET slack_user_id = CASE id WHEN %s THEN 'UX' ELSE 'UY' END WHERE id IN (%s, %s)", (x, x, y))
    try:
        conn = await fetch_one("""INSERT INTO connections (requester_id, helper_id, origin, reason, status, slack_channel_id)
                                  VALUES (%s, %s, 'auto', 'r', 'accepted', 'G1') RETURNING id""", (x, y))
        for user in ("UX", "UX", "UY", "UBOT"):
            await flows.count_message("G1", user)
        row = await fetch_one("SELECT status, message_count FROM connections WHERE id = %s", (conn["id"],))
        assert (row["status"], row["message_count"]) == ("accepted", 3)
        await flows.count_message("G1", "UY")
        row = await fetch_one("SELECT status, message_count, active_at FROM connections WHERE id = %s", (conn["id"],))
        assert (row["status"], row["message_count"]) == ("active", 4) and row["active_at"]
    finally:
        await execute("UPDATE people SET slack_user_id = NULL WHERE id IN (%s, %s)", (x, y))


async def test_feedback_request_and_record(seeded):
    x, y = seeded["planted"]["rate_limit"]
    conn = await fetch_one("""INSERT INTO connections (requester_id, helper_id, origin, reason, status, active_at)
                              VALUES (%s, %s, 'auto', 'r', 'active', now() - INTERVAL '10 minutes') RETURNING id""", (x, y))
    assert await flows.request_due_feedback() >= 1
    assert await flows.request_due_feedback() == 0  # asked once only
    await flows.record_feedback(conn["id"], y, True)
    row = await fetch_one("SELECT helpful_requester, helpful_helper FROM connections WHERE id = %s", (conn["id"],))
    assert (row["helpful_requester"], row["helpful_helper"]) == (None, True)
    ev = await fetch_one("SELECT person_id, value FROM connection_events WHERE connection_id = %s AND event = 'feedback'",
                         (conn["id"],))
    assert (ev["person_id"], ev["value"]) == (y, True)


async def test_slack_channel_mention_creates_task(seeded, fake_muse):
    priya, jordan = seeded["planted"]["recruiting_mlh"]
    fake_muse.handler = muse_for([(priya, 50)])
    await execute("UPDATE people SET slack_user_id = 'UJORDAN' WHERE id = %s", (jordan,))
    try:
        results = await ingest_channel_message({"channel": "C1", "ts": "1790000000.000100", "user": "UOTHER",
                                                "text": "<@UJORDAN> can you run the MLH booth?"})
        assert len(results) == 1 and results[0]["task_id"]
        ev = await fetch_one("SELECT person_id, direction, text FROM activity_events WHERE external_id = %s",
                             (f"C1:1790000000.000100:{jordan}",))
        assert (ev["person_id"], ev["direction"]) == (jordan, "received")
        assert ev["text"].startswith(f"Person {jordan} can you run")
    finally:
        await execute("UPDATE people SET slack_user_id = NULL WHERE id = %s", (jordan,))


def test_gmail_parsing(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_EMAIL_FALLBACK_PERSON", "p_031")
    assert gmail.person_for_recipient("Demo <demo+p_012@gmail.com>") == "p_012"
    assert gmail.person_for_recipient("demo@gmail.com") == "p_031"
    enc = lambda s: base64.urlsafe_b64encode(s.encode()).decode()
    msg = {"id": "m1", "internalDate": "1790000000000", "snippet": "snip", "payload": {
        "mimeType": "multipart/alternative",
        "headers": [{"name": "To", "value": "demo+p_012@gmail.com"}, {"name": "Subject", "value": "MLH booth"}],
        "parts": [{"mimeType": "text/html", "body": {"data": enc("<p>html</p>")}},
                  {"mimeType": "text/plain", "body": {"data": enc("Can you run our booth?")}}]}}
    ev = gmail.to_event(msg)
    assert (ev.person_id, ev.direction, ev.kind, ev.title) == ("p_012", "received", "email", "MLH booth")
    assert ev.text == "MLH booth\nCan you run our booth?"


def test_message_templates():
    m = messages.suggestion(7, "Run the MLH booth", "Priya Shah", "University Recruiting", "Ran MLH booths at 3 events.")
    assert m["text"] == ("New task spotted: *Run the MLH booth*. Priya Shah on University Recruiting "
                         "ran MLH booths at 3 events. Want to connect?")
    assert [e["action_id"] for e in m["blocks"][1]["elements"]] == [messages.CONNECT, messages.NOT_NOW]
    assert m["blocks"][1]["elements"][0]["value"] == "7"
    r = messages.request(7, "Jordan Lee", "Events", "Run the MLH booth", "Ran MLH booths at 3 events.", reason_is_about_helper=True)
    assert "You were suggested because you ran MLH booths at 3 events. Up for a quick chat?" in r["text"]
    ice = messages.icebreaker(7, "Hi both", ["a?", "b?", "c?"], "brief")
    assert ice["blocks"][-1]["elements"][0]["action_id"] == messages.GRAB_15 and len(ice["blocks"]) == 3
