"""Gmail poller (spec §6.2). Demo mail the recipient has READ -> 'received' event -> task pipeline.

It fires when the email is opened (that is when the person learns about the new work), not when it
arrives. Only mail matching GMAIL_QUERY is ever looked at; each message is handled once.

One-time sign-in for the inbox owner (opens a browser):  python -m app.google <DEMO_EMAIL_FALLBACK_PERSON>
"""
import asyncio
import base64
import logging
import re
from datetime import datetime, timezone

from app import google
from app.config import settings
from app.db import fetch_one
from app.schemas import NormalizedEvent

log = logging.getLogger("musketeer.gmail")

PLUS_ADDRESS = re.compile(r"\+(p_\d+)@", re.IGNORECASE)


def person_for_recipient(to_header: str) -> str:
    """demo+p_031@gmail.com -> p_031; anything else -> DEMO_EMAIL_FALLBACK_PERSON."""
    m = PLUS_ADDRESS.search(to_header or "")
    return m.group(1).lower() if m else settings.DEMO_EMAIL_FALLBACK_PERSON


def plain_text(payload: dict) -> str:
    """First text/plain part of a Gmail message payload, decoded."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        if text := plain_text(part):
            return text
    return ""


def _service():
    """The demo inbox is the Google account of DEMO_EMAIL_FALLBACK_PERSON (python -m app.google <id>)."""
    return google.service(settings.DEMO_EMAIL_FALLBACK_PERSON, "gmail", "v1")


def _list_read(svc) -> list[str]:
    q = f"({settings.GMAIL_QUERY}) -is:unread newer_than:1d"
    return [m["id"] for m in svc.users().messages().list(userId="me", q=q, maxResults=20).execute().get("messages", [])]


def _get(svc, msg_id: str) -> dict:
    return svc.users().messages().get(userId="me", id=msg_id, format="full").execute()


def to_event(msg: dict) -> NormalizedEvent:
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    subject = headers.get("subject", "")
    body = plain_text(msg["payload"]) or msg.get("snippet", "")
    return NormalizedEvent(
        source="email", kind="email", external_id=msg["id"],
        time=datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc),
        person_id=person_for_recipient(headers.get("to", "")), direction="received", title=subject,
        text=f"{subject}\n{body[:600]}", metadata={"from": headers.get("from", "")})


async def poll_loop() -> None:
    from app.pipeline.tasks import ingest_received

    if not settings.GMAIL_QUERY.strip():
        # Never read a whole inbox: only ingest mail the demo opted into (spec §1 privacy).
        log.error("Gmail poller not started: set GMAIL_QUERY (e.g. to:you+musketeer@example.com)")
        return
    try:
        svc = await asyncio.to_thread(_service)
    except Exception as e:
        log.error("Gmail poller not started: %s", e)
        return
    log.info("Gmail poller started (every %ss)", settings.GMAIL_POLL_SEC)
    handled: set[str] = set()
    while True:
        try:
            for msg_id in await asyncio.to_thread(_list_read, svc):
                if msg_id in handled:
                    continue
                handled.add(msg_id)
                if await fetch_one("SELECT 1 FROM activity_events WHERE source = 'email' AND external_id = %s", (msg_id,)):
                    continue  # already handled before a restart
                msg = await asyncio.to_thread(_get, svc, msg_id)
                ev = to_event(msg)
                if await fetch_one("SELECT 1 FROM people WHERE id = %s", (ev.person_id,)):
                    result = await ingest_received(ev)
                    log.info("email %s -> %s: task %s", msg["id"], ev.person_id, result and result["task_id"])
                else:
                    log.warning("email %s for unknown person %s; skipped", msg["id"], ev.person_id)
        except Exception:
            log.exception("Gmail poll failed")
        await asyncio.sleep(settings.GMAIL_POLL_SEC)


