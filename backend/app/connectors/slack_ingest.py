"""Public-channel Slack messages (spec §6.3): a 'did' event for a roster sender (expertise evidence), and a
'received' event for each mentioned roster person (task detection).
Connection group DMs are never ingested here; they are only counted (flows.count_message)."""
import logging
import re
from datetime import datetime, timezone

from app import prefs
from app.db import fetch_all, fetch_one
from app.expertise.extract import ingest_did
from app.pipeline.tasks import ingest_received
from app.schemas import NormalizedEvent

log = logging.getLogger("musketeer.slack_ingest")

MENTION = re.compile(r"<@([A-Z0-9]+)>")


async def ingest_channel_message(event: dict) -> list[dict]:
    if not (await prefs.get()).sources.slack:
        return []
    text = event.get("text") or ""
    results = await _ingest_mentions(event, text)
    try:   # after task detection, so the sender's extraction never delays a suggestion
        await _ingest_sender(event, text)
    except Exception:
        log.exception("expertise extraction failed for channel message %s", event.get("ts"))
    return results


async def _ingest_mentions(event: dict, text: str) -> list[dict]:
    mentioned = set(MENTION.findall(text)) - {event.get("user")}
    if not mentioned:
        return []
    people = await fetch_all("SELECT id, name, slack_user_id FROM people WHERE slack_user_id = ANY(%s)", (list(mentioned),))
    names = {p["slack_user_id"]: p["name"] for p in people}
    readable = MENTION.sub(lambda m: names.get(m.group(1), "someone"), text)
    results = []
    for p in people:
        result = await ingest_received(NormalizedEvent(
            source="slack", kind="slack_message", external_id=f"{event['channel']}:{event['ts']}:{p['id']}",
            time=datetime.fromtimestamp(float(event["ts"]), tz=timezone.utc), person_id=p["id"],
            direction="received", title=None, text=readable[:800], metadata={"channel": event["channel"]}))
        if result:
            results.append(result)
    return results


async def _ingest_sender(event: dict, text: str) -> None:
    sender = await fetch_one("SELECT id FROM people WHERE slack_user_id = %s", (event.get("user"),))
    if sender and text.strip():
        await ingest_did(NormalizedEvent(
            source="slack", kind="slack_message", external_id=f"{event['channel']}:{event['ts']}",
            time=datetime.fromtimestamp(float(event["ts"]), tz=timezone.utc), person_id=sender["id"],
            direction="did", title=None, text=MENTION.sub("someone", text)[:800], metadata={"channel": event["channel"]}))
