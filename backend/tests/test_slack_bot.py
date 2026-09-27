"""Drives the real Slack handlers (slack_bot.on_button / on_request_form) through the whole demo:
suggestion -> Connect -> request form -> Sure -> group chat -> meeting proposal -> Another time -> both tap Book it.
Also: buttons from before a reset, and a button whose step crashes, both answer instead of going silent."""
import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.bot import flows, messages
from app.db import execute, fetch_one

TZ = ZoneInfo("America/New_York")
SLOT = datetime(2026, 10, 5, 14, 0, tzinfo=TZ)


class FakeClient:
    """Records every Slack Web API call made by the bot."""
    def __init__(self):
        self.calls = []

    def __getattr__(self, method):
        async def call(**kw):
            self.calls.append((method, kw))
            return {"ok": True, "channel": {"id": "GDEMO"}}
        return call

    def posts(self, channel: str) -> list[dict]:
        return [kw for m, kw in self.calls if m == "chat_postMessage" and kw["channel"] == channel]


class Respond:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kw):
        self.calls.append(kw)


async def noop_ack(**kw):
    pass


def button(message: dict, action_id: str) -> dict:
    for block in message["blocks"]:
        for el in block.get("elements", []):
            if block["type"] == "actions" and el["action_id"] == action_id:
                return el
    raise AssertionError(f"no {action_id} button in: {message['text']}")


async def click(slack_bot, client, user: str, message: dict, action_id: str) -> Respond:
    el = button(message, action_id)
    respond = Respond()
    body = {"user": {"id": user}, "message": {"text": message["text"], "ts": "1.0"},
            "channel": {"id": "DCHAN"}, "trigger_id": "TRIGGER"}
    await slack_bot.on_button(ack=noop_ack, body=body, action={"action_id": el["action_id"], "value": el["value"]},
                              respond=respond, say=None, client=client)
    await asyncio.sleep(0.05)   # let spawned background steps run
    return respond


@pytest.fixture
async def demo(seeded, fake_muse, monkeypatch):
    from app.bot import slack_bot   # imported here: its Socket Mode handler needs a running loop

    client = FakeClient()
    monkeypatch.setattr(flows, "_slack", lambda: client)
    fake_muse.handler = lambda prompt, schema: {"message": "Meet each other.", "questions": ["Q1?", "Q2?", "Q3?"]}

    async def find(ids, skip=0):
        return SLOT + timedelta(hours=skip), SLOT + timedelta(hours=skip, minutes=30)

    async def book(org, guest, s, e, title, desc):
        booked.append((org, guest, s))
        return {"htmlLink": "https://calendar.google.com/event?eid=x", "hangoutLink": "https://meet.google.com/x"}

    async def start_meeting(*a, **kw):
        started.append(a[0])

    async def not_busy(pid):
        return None

    booked, started = [], []
    monkeypatch.setattr(flows.google, "has_account", lambda pid: True)
    monkeypatch.setattr(flows.calendar, "busy_until", not_busy)
    monkeypatch.setattr(flows.calendar, "find_common_slot", find)
    monkeypatch.setattr(flows.calendar, "book", book)
    monkeypatch.setattr("app.bot.meetings.start_meeting", start_meeting)

    helper, requester = seeded["planted"]["recruiting_mlh"]
    await execute("UPDATE people SET slack_user_id = CASE id WHEN %s THEN 'UREQ' ELSE 'UHELP' END WHERE id IN (%s, %s)",
                  (requester, requester, helper))
    conn_id, delivered = await flows.open_connection(requester, helper, "manager_nudge", "Both run hackathon booths.")
    assert delivered == "slack"
    yield slack_bot, client, conn_id, booked, started
    await execute("DELETE FROM connections WHERE id = %s", (conn_id,))
    await execute("UPDATE people SET slack_user_id = NULL WHERE id IN (%s, %s)", (requester, helper))


async def next_post(client: FakeClient, channel: str, before: int) -> dict:
    """Background steps post to Slack a moment later; wait for the new message."""
    for _ in range(100):
        if len(client.posts(channel)) > before:
            return client.posts(channel)[-1]
        await asyncio.sleep(0.05)
    raise AssertionError(f"nothing new posted to {channel}")


async def status(conn_id: int) -> str:
    return (await fetch_one("SELECT status FROM connections WHERE id = %s", (conn_id,)))["status"]


async def test_full_demo_through_the_slack_handlers(demo):
    slack_bot, client, conn_id, booked, started = demo

    # 1. Requester taps Connect: a form opens and the helper hears nothing yet.
    suggestion = client.posts("UREQ")[-1]
    await click(slack_bot, client, "UREQ", suggestion, messages.CONNECT)
    [(method, view)] = [(m, kw) for m, kw in client.calls if m == "views_open"]
    assert view["trigger_id"] == "TRIGGER" and view["view"]["callback_id"] == messages.REQUEST_FORM
    assert client.posts("UHELP") == [] and await status(conn_id) == "suggested"

    # 2. Submits the form blank: the standard request goes to the helper.
    await slack_bot.on_request_form(ack=noop_ack, view={**view["view"], "state": {"values": {}}}, client=client)
    assert await status(conn_id) == "requested"
    request = client.posts("UHELP")[-1]

    # 3. Helper taps Sure: group DM with an icebreaker and a Grab 15 min button.
    await click(slack_bot, client, "UHELP", request, messages.SURE)
    assert await status(conn_id) == "accepted"
    icebreaker = client.posts("GDEMO")[-1]
    button(icebreaker, messages.GRAB_15)

    # 4. A short exchange (both speak, 4 messages) goes active and proposes a time on its own.
    n = len(client.posts("GDEMO"))
    for user in ("UREQ", "UHELP", "UHELP", "UHELP"):
        await slack_bot.on_message({"channel_type": "mpim", "channel": "GDEMO", "user": user})
    proposal = await next_post(client, "GDEMO", n)
    assert await status(conn_id) == "active"
    assert "Mon Oct 5, 2:00–2:30 PM" in proposal["text"]

    # 5. Another time -> the next slot.
    n = len(client.posts("GDEMO"))
    r = await click(slack_bot, client, "UREQ", proposal, messages.OTHER_TIME)
    assert "Looking for another time" in r.calls[0]["text"]
    proposal = await next_post(client, "GDEMO", n)
    assert "Mon Oct 5, 3:00–3:30 PM" in proposal["text"]

    # 6. Grab 15 min still works after that and answers right away.
    n = len(client.posts("GDEMO"))
    r = await click(slack_bot, client, "UHELP", icebreaker, messages.GRAB_15)
    assert "Finding a time" in r.calls[0]["text"]
    assert "you're both free" in (await next_post(client, "GDEMO", n))["text"]

    # 7. Book it needs both people: the first tap waits, the second books the calendar invite.
    r = await click(slack_bot, client, "UREQ", proposal, messages.BOOK)
    assert booked == [] and "is in. Waiting for" in r.calls[0]["text"]
    n = len(client.posts("GDEMO"))
    await click(slack_bot, client, "UHELP", r.calls[0], messages.BOOK)
    assert "Booked *Mon Oct 5, 3:00–3:30 PM*" in (await next_post(client, "GDEMO", n))["text"]
    assert [s for _, _, s in booked] == [SLOT + timedelta(hours=1)] and started == [conn_id]


async def test_button_from_before_a_reset_says_so(demo):
    slack_bot, client, conn_id, *_ = demo
    stale = messages.icebreaker(999999, "Hi", ["Q?"])
    r = await click(slack_bot, client, "UREQ", stale, messages.GRAB_15)
    assert "was reset" in r.calls[0]["text"]


async def test_crashing_button_answers_instead_of_going_silent(demo, monkeypatch):
    slack_bot, client, conn_id, *_ = demo

    async def boom(*a, **kw):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(flows, "propose_meeting", boom)
    r = await click(slack_bot, client, "UREQ", messages.icebreaker(conn_id, "Hi", ["Q?"]), messages.GRAB_15)
    assert "something went wrong" in r.calls[-1]["text"] and r.calls[-1]["response_type"] == "ephemeral"


def test_every_bot_button_has_a_handler():
    """Every musketeer_ action id the templates can produce is routed in slack_bot._on_button."""
    import inspect
    import re

    from app.bot import slack_bot
    src = inspect.getsource(slack_bot)
    names = [k for k, v in vars(messages).items() if k.isupper() and isinstance(v, str) and v.startswith("musketeer_")]
    assert len(names) >= 8
    for name in names:
        assert re.search(rf"messages\.{name}\b", src), f"{name} is never handled"
