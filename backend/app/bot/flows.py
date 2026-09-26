"""Connection state machine (spec §10), shared by Slack buttons, simulated deliveries and dashboard actions.

suggested --requester yes--> requested --helper yes--> accepted --both sent >= 2 msgs--> active
    '--requester no--> declined         '--helper no--> declined (-> next candidate, §8.4 step 7)

Delivery rule: people with slack_user_id (and ENABLE_SLACK) get real Slack messages. Others get
simulated delivery: logged, auto-"yes" after SIMULATED_ACCEPT_SEC; a simulated chat turns active
10 s after acceptance with message_count = 4.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.ai.muse import muse_json
from datetime import datetime, timedelta

from app import google, prefs
from app.bot import calendar, messages
from app.config import settings
from app.db import execute, fetch_all, fetch_one
from app.schemas import IcebreakerOut

log = logging.getLogger("musketeer.flows")

SIMULATED_ACTIVE_AFTER_SEC = 10
SIMULATED_MESSAGE_COUNT = 4
_background: set[asyncio.Task] = set()


def spawn(fn: Callable[[], Awaitable], delay: float = 0) -> None:
    """Run fn() after delay seconds in the background, logging failures."""
    async def run():
        await asyncio.sleep(delay)
        try:
            await fn()
        except Exception:
            log.exception("background flow step failed")
    task = asyncio.create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)


def cancel_background() -> None:
    for t in list(_background):
        t.cancel()


def _slack():
    if not settings.ENABLE_SLACK:
        return None
    from app.bot.slack_bot import app
    return app.client


async def person(pid: str) -> dict:
    return await fetch_one("""
        SELECT p.id, p.name, p.title, p.summary, p.slack_user_id, p.team_id, t.name AS team_name
        FROM people p JOIN teams t ON t.id = p.team_id WHERE p.id = %s""", (pid,))


def first_name(p: dict) -> str:
    return p["name"].split()[0]


async def deliver(to: dict, message: dict, on_simulated_yes: Callable[[], Awaitable] | None = None) -> str:
    client = _slack()
    if client and to["slack_user_id"]:
        await client.chat_postMessage(channel=to["slack_user_id"], **message)
        return "slack"
    if not settings.SIMULATE_UNMAPPED:
        log.warning("cannot deliver to %s (no Slack mapping, simulation off)", to["id"])
        return "undelivered"
    log.info("simulated delivery to %s: %s", to["id"], message["text"])
    if on_simulated_yes:
        spawn(on_simulated_yes, settings.SIMULATED_ACCEPT_SEC)
    return "simulated"


_TIMESTAMP_COL = {"accepted": "accepted_at", "active": "active_at", "declined": "closed_at", "expired": "closed_at"}


async def transition(conn_id: int, to: str, allowed_from: tuple[str, ...], person_id: str | None = None,
                     **extra) -> dict | None:
    """Move a connection to `to` only if it is currently in `allowed_from`. Returns the row, or None if not allowed."""
    sets = ["status = %(to)s"] + [f"{k} = %({k})s" for k in extra]
    if to in _TIMESTAMP_COL:
        sets.append(f"{_TIMESTAMP_COL[to]} = now()")
    row = await fetch_one(f"""UPDATE connections SET {', '.join(sets)}
                              WHERE id = %(id)s AND status = ANY(%(allowed)s) RETURNING *""",
                          {"id": conn_id, "to": to, "allowed": list(allowed_from), **extra})
    if row:
        await execute("INSERT INTO connection_events (connection_id, event, person_id) VALUES (%s, %s, %s)",
                      (conn_id, to, person_id))
    else:
        log.info("ignored transition of connection %s to %s", conn_id, to)
    return row


async def open_connection(requester_id: str, helper_id: str, origin: str, reason: str, *,
                          match_score: float | None = None, task_id: int | None = None,
                          initiated_by: str | None = None) -> tuple[int, str]:
    """Create a `suggested` connection and send the first prompt to the requester. Returns (id, delivered)."""
    row = await fetch_one("""
        INSERT INTO connections (requester_id, helper_id, task_id, origin, initiated_by, reason, match_score, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'suggested') RETURNING *""",
        (requester_id, helper_id, task_id, origin, initiated_by, reason, match_score))
    await execute("INSERT INTO connection_events (connection_id, event) VALUES (%s, 'suggested')", (row["id"],))

    requester, helper = await person(requester_id), await person(helper_id)
    if origin == "auto":
        task = await fetch_one("SELECT summary FROM tasks WHERE id = %s", (task_id,))
        msg = messages.suggestion(row["id"], task["summary"], helper["name"], helper["team_name"], reason)
    else:
        manager = await person(initiated_by) if initiated_by else {"name": "Musketeer"}
        if origin == "manager_nudge":
            msg = messages.nudge(row["id"], manager["name"], helper["name"], helper["team_name"], reason)
        else:
            msg = messages.lead_intro(row["id"], manager["name"], helper["name"], helper["team_name"], reason)
    delivered = await deliver(requester, msg, on_simulated_yes=lambda: requester_accepts(row["id"]))
    return row["id"], delivered


async def _topic(conn: dict, requester: dict) -> str:
    """What the requester is working on, for the helper's request message."""
    if conn["task_id"]:
        return (await fetch_one("SELECT summary FROM tasks WHERE id = %s", (conn["task_id"],)))["summary"]
    if conn["origin"] == "lead_intro":
        helper = await person(conn["helper_id"])
        return f"an intro between {requester['team_name']} and {helper['team_name']}"
    return "work similar to yours"


async def request_draft(conn_id: int) -> tuple[str, str] | None:
    """(helper first name, prefilled message) for the Connect form, or None if the suggestion is no longer open."""
    conn = await fetch_one("SELECT * FROM connections WHERE id = %s AND status = 'suggested'", (conn_id,))
    if not conn:
        return None
    requester, helper = await person(conn["requester_id"]), await person(conn["helper_id"])
    return first_name(helper), messages.request_draft(first_name(helper), lf_topic(await _topic(conn, requester)))


def lf_topic(topic: str) -> str:
    return topic[:1].lower() + topic[1:] if topic[:2] != topic[:2].upper() else topic


async def requester_accepts(conn_id: int, note: str | None = None, links: list[str] | None = None) -> None:
    """The requester is ready: store what they wrote (if anything) and send the request to the helper."""
    conn = await fetch_one("SELECT requester_id FROM connections WHERE id = %s", (conn_id,))
    conn = await transition(conn_id, "requested", ("suggested",), person_id=conn["requester_id"],
                            request_note=note, request_links=links or [])
    if not conn:
        return
    requester, helper = await person(conn["requester_id"]), await person(conn["helper_id"])
    p = await prefs.get()
    if p.wait_for_busy and p.sources.calendar and google.has_account(helper["id"]):
        try:
            free_at = await calendar.busy_until(helper["id"])
        except Exception:
            log.exception("free/busy check failed; sending now")
            free_at = None
        if free_at:
            # Respect meetings and focus time: hold the request and tell the requester when it goes out.
            await deliver(requester, messages.request_held(first_name(helper), free_at))
            spawn(lambda: send_request(conn_id), (free_at - datetime.now(free_at.tzinfo)).total_seconds())
            return
    await send_request(conn_id)


async def send_request(conn_id: int) -> None:
    conn = await fetch_one("SELECT * FROM connections WHERE id = %s", (conn_id,))
    if conn["status"] != "requested":
        return
    requester, helper = await person(conn["requester_id"]), await person(conn["helper_id"])
    msg = messages.request(conn_id, requester["name"], requester["team_name"], await _topic(conn, requester),
                           conn["reason"], reason_is_about_helper=conn["origin"] == "auto",
                           note=conn["request_note"], links=conn["request_links"])
    await deliver(helper, msg, on_simulated_yes=lambda: helper_accepts(conn_id))


async def requester_declines(conn_id: int) -> None:
    conn = await fetch_one("SELECT requester_id FROM connections WHERE id = %s", (conn_id,))
    conn = await transition(conn_id, "declined", ("suggested",), person_id=conn["requester_id"])
    if conn and conn["origin"] == "auto" and conn["task_id"]:
        await execute("UPDATE tasks SET status = 'dismissed' WHERE id = %s", (conn["task_id"],))


async def _icebreaker(conn: dict, requester: dict, helper: dict) -> IcebreakerOut:
    describe = lambda p: f"{p['name']}, {p['title']}, {p['team_name']}: {p['summary'] or ''}"
    try:
        return await muse_json("icebreaker", {
            "requester": describe(requester), "helper": describe(helper),
            "task_summary": await _topic(conn, requester), "reason": conn["reason"]}, IcebreakerOut, cache=False)
    except Exception as e:  # the group DM must still happen on camera
        log.warning("icebreaker unavailable, using template: %s", e)
        return IcebreakerOut(
            message=f"{first_name(requester)}, meet {first_name(helper)}. {conn['reason']}",
            questions=["What would you do differently next time?", "What's the first thing to get right?",
                       "Who else should I talk to?"])


async def helper_accepts(conn_id: int) -> None:
    conn = await fetch_one("SELECT helper_id FROM connections WHERE id = %s", (conn_id,))
    conn = await transition(conn_id, "accepted", ("requested",), person_id=conn["helper_id"])
    if not conn:
        return
    if conn["task_id"]:
        await execute("UPDATE tasks SET status = 'connected' WHERE id = %s", (conn["task_id"],))
    requester, helper = await person(conn["requester_id"]), await person(conn["helper_id"])

    client = _slack()
    if client and requester["slack_user_id"] and helper["slack_user_id"]:
        opened = await client.conversations_open(users=f"{requester['slack_user_id']},{helper['slack_user_id']}")
        channel = opened["channel"]["id"]
        ice = await _icebreaker(conn, requester, helper)
        brief = None
        if conn["origin"] == "lead_intro":
            row = await fetch_one("""SELECT lead_brief FROM team_overlaps
                                     WHERE team_a = LEAST(%(a)s, %(b)s) AND team_b = GREATEST(%(a)s, %(b)s)""",
                                  {"a": requester["team_id"], "b": helper["team_id"]})
            brief = row and row["lead_brief"]
        await client.chat_postMessage(channel=channel, **messages.icebreaker(conn_id, ice.message, ice.questions, brief))
        await execute("UPDATE connections SET slack_channel_id = %s WHERE id = %s", (channel, conn_id))
    else:
        log.info("simulated group DM for connection %s", conn_id)
        spawn(lambda: _simulate_chat(conn_id), SIMULATED_ACTIVE_AFTER_SEC)


async def _simulate_chat(conn_id: int) -> None:
    conn = await fetch_one("SELECT requester_id, helper_id, status FROM connections WHERE id = %s", (conn_id,))
    if not conn or conn["status"] != "accepted":
        return
    for i in range(SIMULATED_MESSAGE_COUNT):
        await execute("INSERT INTO connection_events (connection_id, event, person_id) VALUES (%s, 'message', %s)",
                      (conn_id, conn["requester_id"] if i % 2 == 0 else conn["helper_id"]))
    await transition(conn_id, "active", ("accepted",), message_count=SIMULATED_MESSAGE_COUNT)


async def helper_declines(conn_id: int) -> None:
    conn = await fetch_one("SELECT helper_id FROM connections WHERE id = %s", (conn_id,))
    conn = await transition(conn_id, "declined", ("requested",), person_id=conn["helper_id"])
    if not conn:
        return
    helper = await person(conn["helper_id"])
    await deliver(await person(conn["requester_id"]), messages.helper_cant(first_name(helper)))
    if conn["origin"] == "auto" and conn["task_id"]:
        from app.pipeline.tasks import retry_next_candidate
        await retry_next_candidate(conn["task_id"])


async def count_message(channel_id: str, slack_user_id: str) -> None:
    """A message in a connection's group DM: count it (never store content)."""
    conn = await fetch_one("""SELECT c.id, c.status, p.id AS person_id FROM connections c
                              JOIN people p ON p.slack_user_id = %s AND p.id IN (c.requester_id, c.helper_id)
                              WHERE c.slack_channel_id = %s AND c.status IN ('accepted', 'active')""",
                           (slack_user_id, channel_id))
    if not conn:
        return
    await execute("UPDATE connections SET message_count = message_count + 1 WHERE id = %s", (conn["id"],))
    await execute("INSERT INTO connection_events (connection_id, event, person_id) VALUES (%s, 'message', %s)",
                  (conn["id"], conn["person_id"]))
    if conn["status"] == "accepted":
        senders = await fetch_all("""SELECT person_id FROM connection_events WHERE connection_id = %s AND event = 'message'
                                     GROUP BY person_id HAVING count(*) >= 2""", (conn["id"],))
        if len(senders) >= 2 and await transition(conn["id"], "active", ("accepted",)):
            spawn(lambda: propose_meeting(conn["id"]))  # after a short exchange, offer a time that works for both


async def request_due_feedback() -> int:
    """Ask both people 'was this helpful?' feedback_after_min (Settings) after a connection went active."""
    due = await fetch_all("""UPDATE connections SET feedback_requested_at = now()
                             WHERE status = 'active' AND feedback_requested_at IS NULL
                               AND active_at < now() - make_interval(mins => %s)
                             RETURNING id, requester_id, helper_id""", ((await prefs.get()).feedback_after_min,))
    for c in due:
        requester, helper = await person(c["requester_id"]), await person(c["helper_id"])
        await deliver(requester, messages.feedback(c["id"], helper["name"]))
        await deliver(helper, messages.feedback(c["id"], requester["name"]))
    return len(due)


async def record_feedback(conn_id: int, person_id: str, helpful: bool) -> None:
    row = await fetch_one("""UPDATE connections SET
                               helpful_requester = CASE WHEN requester_id = %(p)s THEN %(v)s ELSE helpful_requester END,
                               helpful_helper = CASE WHEN helper_id = %(p)s THEN %(v)s ELSE helpful_helper END
                             WHERE id = %(id)s AND %(p)s IN (requester_id, helper_id) RETURNING id""",
                          {"id": conn_id, "p": person_id, "v": helpful})
    if row:
        await execute("""INSERT INTO connection_events (connection_id, event, person_id, value)
                         VALUES (%s, 'feedback', %s, %s)""", (conn_id, person_id, helpful))


async def propose_meeting(conn_id: int, skip: int = 0, follow_up_of: int | None = None) -> None:
    """Post a slot that is free on both Google Calendars in the group DM (stub text if calendars aren't linked).
    follow_up_of: the meeting this one follows up on; its notes carry into the new page."""
    conn = await fetch_one("SELECT requester_id, helper_id, slack_channel_id FROM connections WHERE id = %s", (conn_id,))
    client = _slack()
    if not client or not conn or not conn["slack_channel_id"]:
        return
    channel, ids = conn["slack_channel_id"], [conn["requester_id"], conn["helper_id"]]
    if not (await prefs.get()).sources.calendar or not all(google.has_account(p) for p in ids):
        await client.chat_postMessage(channel=channel, text=messages.GRAB_15_REPLY)
        return
    try:
        slot = await calendar.find_common_slot(ids, skip)
    except Exception:
        log.exception("calendar lookup failed for connection %s", conn_id)
        await client.chat_postMessage(channel=channel, text=messages.GRAB_15_REPLY)
        return
    if not slot:
        await client.chat_postMessage(channel=channel, **messages.NO_COMMON_TIME)
        return
    requester, helper = await person(ids[0]), await person(ids[1])
    await client.chat_postMessage(channel=channel, **messages.meeting_proposal(
        conn_id, first_name(requester), first_name(helper), *slot, skip, follow_up_of=follow_up_of))


async def book_meeting(conn_id: int, start_iso: str, follow_up_of: int | None = None) -> None:
    from app.bot import meetings

    conn = await fetch_one("SELECT * FROM connections WHERE id = %s", (conn_id,))
    requester, helper = await person(conn["requester_id"]), await person(conn["helper_id"])
    start = datetime.fromisoformat(start_iso)
    end = start + timedelta(minutes=settings.MEETING_MINUTES)
    title = f"{'Follow-up: ' if follow_up_of else ''}{first_name(requester)} + {first_name(helper)}: {await _topic(conn, requester)}"
    event = await calendar.book(requester["id"], helper["id"], start, end, title[:120],
                                f"{conn['reason']}\n\nSet up by Musketeer.")
    await _slack().chat_postMessage(channel=conn["slack_channel_id"], **messages.meeting_booked(start, end, event["htmlLink"]))
    await meetings.start_meeting(conn_id, start, end, event, parent_id=follow_up_of)


async def approve_meeting(conn_id: int, start_iso: str, skip: int, approved_by: str, clicker_id: str | None,
                          follow_up_of: int | None = None) -> dict | None:
    """Book it needs both people: the first tap records them, the other person's tap books.
    Returns the replacement for the proposal message, or None to leave it unchanged."""
    conn = await fetch_one("SELECT requester_id, helper_id FROM connections WHERE id = %s", (conn_id,))
    ids = [conn["requester_id"], conn["helper_id"]] if conn else []
    if clicker_id not in ids:
        return None  # only the two people in the chat can confirm
    requester, helper = await person(ids[0]), await person(ids[1])
    start = datetime.fromisoformat(start_iso)
    end = start + timedelta(minutes=settings.MEETING_MINUTES)
    if approved_by in ids and approved_by != clicker_id:
        spawn(lambda: book_meeting(conn_id, start_iso, follow_up_of))
        return {"text": f"Both confirmed *{messages.when(start, end)}*. Booking it now…"}
    me, other = (requester, helper) if clicker_id == ids[0] else (helper, requester)
    return messages.meeting_proposal(conn_id, first_name(requester), first_name(helper), start, end, skip,
                                     approved_by=clicker_id, approved_first=first_name(me), waiting_for=first_name(other),
                                     follow_up_of=follow_up_of)
