from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.bot import calendar, flows, messages
from app.config import settings
from app.db import execute, fetch_one

TZ = ZoneInfo("America/New_York")


def test_candidate_slots_are_weekday_working_hours():
    fri_4pm = datetime(2026, 10, 2, 16, 10, tzinfo=TZ)  # Friday
    slots = list(calendar.candidate_slots(fri_4pm, 30, TZ, days=4))
    first = slots[0][0]
    assert first == datetime(2026, 10, 2, 17, 30, tzinfo=TZ)  # >= 1h ahead, :00/:30 aligned
    assert all(s.weekday() < 5 and s.hour >= 9 and e.hour * 60 + e.minute <= 18 * 60 for s, e in slots)
    assert slots[1][0] == datetime(2026, 10, 5, 9, 0, tzinfo=TZ)  # skips the weekend


def test_first_free_respects_everyone_and_skip():
    day = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)
    slots = [(day + timedelta(minutes=30 * i), day + timedelta(minutes=30 * i + 30)) for i in range(6)]
    busy_a = [(day, day + timedelta(hours=1))]                                           # 9:00-10:00
    busy_b = [(day + timedelta(hours=1, minutes=30), day + timedelta(hours=2))]          # 10:30-11:00
    assert calendar.first_free([busy_a, busy_b], slots) == slots[2]                       # 10:00
    assert calendar.first_free([busy_a, busy_b], slots, skip=1) == slots[4]               # 11:00
    assert calendar.first_free([[(day, day + timedelta(hours=9))]], slots) is None


def test_when_format():
    s = datetime(2026, 9, 29, 14, 30, tzinfo=TZ)
    assert messages.when(s, s + timedelta(minutes=30)) == "Tue Sep 29, 2:30–3:00 PM"


class FakeSlack:
    def __init__(self):
        self.posts = []

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)


@pytest.fixture
async def group_dm(seeded, monkeypatch):
    slack = FakeSlack()
    monkeypatch.setattr(flows, "_slack", lambda: slack)
    a, b = seeded["planted"]["recruiting_mlh"]
    conn = await fetch_one("""INSERT INTO connections (requester_id, helper_id, origin, reason, status, slack_channel_id)
                              VALUES (%s, %s, 'lead_intro', 'Both run hackathon booths.', 'active', 'G9') RETURNING id""", (b, a))
    yield conn["id"], slack, (b, a)
    await execute("DELETE FROM connections WHERE id = %s", (conn["id"],))


async def test_propose_without_linked_calendars_falls_back(group_dm, monkeypatch):
    conn_id, slack, _ = group_dm
    monkeypatch.setattr(flows.google, "has_account", lambda pid: False)
    await flows.propose_meeting(conn_id)
    assert slack.posts == [{"channel": "G9", "text": messages.GRAB_15_REPLY}]


async def test_propose_then_other_time_then_book(group_dm, monkeypatch):
    conn_id, slack, (requester, helper) = group_dm
    start = datetime(2026, 10, 5, 14, 0, tzinfo=TZ)
    asked, booked = [], []

    async def fake_find(ids, skip=0):
        asked.append((ids, skip))
        return start + timedelta(hours=skip), start + timedelta(hours=skip, minutes=30)

    async def fake_book(org, guest, s, e, title, desc):
        booked.append((org, guest, s, e, title, desc))
        return {"htmlLink": "https://calendar.google.com/event?eid=x", "hangoutLink": "https://meet.google.com/x"}

    monkeypatch.setattr(flows.google, "has_account", lambda pid: True)
    monkeypatch.setattr(flows.calendar, "find_common_slot", fake_find)
    monkeypatch.setattr(flows.calendar, "book", fake_book)
    monkeypatch.setattr(settings, "MEETING_MINUTES", 30)

    await flows.propose_meeting(conn_id)
    await flows.propose_meeting(conn_id, skip=1)
    assert asked == [([requester, helper], 0), ([requester, helper], 1)]
    book_btn, other_btn = slack.posts[1]["blocks"][1]["elements"]
    assert "you're both free *Mon Oct 5, 3:00–3:30 PM*" in slack.posts[1]["text"]
    assert other_btn["value"] == f"{conn_id}|2"

    _, start_iso, _, approved, _ = book_btn["value"].split("|")
    assert approved == ""
    started = []
    async def fake_start(*a, **kw):
        started.append(kw.get("parent_id"))
    monkeypatch.setattr("app.bot.meetings.start_meeting", fake_start)
    await flows.book_meeting(conn_id, start_iso)
    assert started == [None]  # the shared meeting page gets created
    org, guest, s, e, title, desc = booked[0]
    assert (org, guest) == (requester, helper) and e - s == timedelta(minutes=30)
    assert title.startswith("Person + Person: an intro between")  # synthetic names are "Person p_0xx"
    assert desc.endswith("Set up by Musketeer.")
    assert "Booked *Mon Oct 5, 3:00–3:30 PM*" in slack.posts[-1]["text"]


async def test_chat_going_active_proposes_a_meeting(seeded, monkeypatch):
    proposed = []

    async def fake_propose(conn_id, skip=0):
        proposed.append(conn_id)

    monkeypatch.setattr(flows, "propose_meeting", fake_propose)
    x, y = seeded["planted"]["rate_limit"]
    await execute("UPDATE people SET slack_user_id = CASE id WHEN %s THEN 'UA' ELSE 'UB' END WHERE id IN (%s, %s)", (x, x, y))
    conn = await fetch_one("""INSERT INTO connections (requester_id, helper_id, origin, reason, status, slack_channel_id)
                              VALUES (%s, %s, 'auto', 'r', 'accepted', 'G7') RETURNING id""", (x, y))
    try:
        for user in ("UA", "UB", "UA", "UB"):
            await flows.count_message("G7", user)
        await fetch_one("SELECT 1")  # let the spawned proposal run
        import asyncio
        await asyncio.sleep(0.05)
        assert proposed == [conn["id"]]
    finally:
        await execute("DELETE FROM connections WHERE id = %s", (conn["id"],))
        await execute("UPDATE people SET slack_user_id = NULL WHERE id IN (%s, %s)", (x, y))


async def test_booking_needs_both_people(monkeypatch):
    """No DB: the first Book it tap records the person, only the other person's tap books."""
    people = {"p_a": {"id": "p_a", "name": "Anthony N"}, "p_b": {"id": "p_b", "name": "Andy S"}}

    async def fake_fetch_one(sql, params=None):
        return {"requester_id": "p_a", "helper_id": "p_b"}

    async def fake_person(pid):
        return people[pid]

    booked = []
    monkeypatch.setattr(flows, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(flows, "person", fake_person)
    monkeypatch.setattr(flows, "spawn", lambda fn, delay=0: booked.append(fn))
    start = datetime(2026, 10, 5, 14, 0, tzinfo=TZ).isoformat()

    assert await flows.approve_meeting(1, start, 0, "", "p_zzz") is None          # outsider: ignored
    first = await flows.approve_meeting(1, start, 0, "", "p_b")                     # Andy taps first
    assert "✓ Andy is in. Waiting for Anthony." in first["text"] and not booked
    book_value = first["blocks"][1]["elements"][0]["value"]
    assert book_value.endswith("|p_b|")  # approver, then (empty) follow-up-of
    again = await flows.approve_meeting(1, start, 0, "p_b", "p_b")                  # Andy again: still waiting
    assert "Waiting for Anthony" in again["text"] and not booked
    done = await flows.approve_meeting(1, start, 0, "p_b", "p_a")                   # Anthony confirms
    assert done["text"].startswith("Both confirmed") and len(booked) == 1
