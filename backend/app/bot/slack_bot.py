"""Bolt app over Socket Mode (spec §10): buttons, group-DM message counting, channel ingest."""
import logging
import re

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from app.bot import flows, meetings, messages
from app.config import settings
from app.db import fetch_one

log = logging.getLogger("musketeer.slack")

app = AsyncApp(token=settings.SLACK_BOT_TOKEN)
handler = AsyncSocketModeHandler(app, settings.SLACK_APP_TOKEN)
meetings.register(app)  # shared meeting notes canvas, deliverable, next steps, follow-up

_ACTIONS = {
    messages.CONNECT: ("Connecting…", flows.requester_accepts),
    messages.NOT_NOW: ("Not now", flows.requester_declines),
    messages.SURE: ("Sure! Opening a group chat…", flows.helper_accepts),
    messages.CANT: ("Can't right now", flows.helper_declines),
}


@app.event("message")
async def on_message(event):
    if event.get("bot_id") or event.get("subtype"):
        return
    channel_type = event.get("channel_type")
    # Never log or store message text from group DMs (spec §1 privacy).
    log.info("message channel_type=%s user=%s channel=%s", channel_type, event.get("user"), event.get("channel"))
    if channel_type == "mpim":
        await flows.count_message(event["channel"], event["user"])
    elif channel_type == "channel":
        from app.connectors.slack_ingest import ingest_channel_message
        await ingest_channel_message(event)


@app.action(re.compile(r"^musketeer_"))
async def on_button(ack, body, action, respond, say):
    await ack()
    action_id, *parts = [action["action_id"], *action["value"].split("|")]
    conn_id = int(parts[0])
    original = body["message"]["text"]

    if action_id in _ACTIONS:
        label, step = _ACTIONS[action_id]
        await respond(replace_original=True, text=f"{original}\n_{label}_")
        await step(conn_id)
    elif action_id == messages.GRAB_15:
        await flows.propose_meeting(conn_id)
    elif action_id == messages.OTHER_TIME:
        await respond(replace_original=True, text=f"{original}\n_Looking for another time…_")
        follow_up_of = int(parts[2]) if len(parts) > 2 and parts[2] else None
        await flows.propose_meeting(conn_id, skip=int(parts[1]), follow_up_of=follow_up_of)
    elif action_id == messages.BOOK:
        person = await fetch_one("SELECT id FROM people WHERE slack_user_id = %s", (body["user"]["id"],))
        parts += ["0", "", ""][len(parts) - 2:]  # older buttons carry fewer fields
        start_iso, skip, approved_by = parts[1], int(parts[2]), parts[3]
        follow_up_of = int(parts[4]) if parts[4] else None
        update = await flows.approve_meeting(conn_id, start_iso, skip, approved_by, person and person["id"], follow_up_of)
        if update:
            await respond(replace_original=True, **update)
    elif action_id in (messages.HELPFUL_YES, messages.HELPFUL_NO):
        person = await fetch_one("SELECT id FROM people WHERE slack_user_id = %s", (body["user"]["id"],))
        if person:
            await flows.record_feedback(conn_id, person["id"], action_id == messages.HELPFUL_YES)
        await respond(replace_original=True, text=f"{original}\n_Thanks for the feedback!_")


async def start() -> None:
    await handler.connect_async()
    log.info("Slack Socket Mode connected")


async def stop() -> None:
    await handler.close_async()
