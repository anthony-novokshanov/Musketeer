"""Public-channel Slack messages that mention a roster person -> 'received' event for them (spec §6.3).
Connection group DMs are never ingested here; they are only counted (flows.count_message)."""
import re
from datetime import datetime, timezone

from app import prefs
from app.db import fetch_all
from app.pipeline.tasks import ingest_received
from app.schemas import NormalizedEvent

MENTION = re.compile(r"<@([A-Z0-9]+)>")


async def ingest_channel_message(event: dict) -> list[dict]:
    if not (await prefs.get()).sources.slack:
        return []
    text = event.get("text") or ""
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
