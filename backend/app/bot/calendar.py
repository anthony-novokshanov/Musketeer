"""Find a time that works for both people (Google Calendar free/busy) and book it with a Meet link."""
import asyncio
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import google
from app.config import settings

WORKDAY = (time(9, 0), time(18, 0))
SEARCH_DAYS = 7


def candidate_slots(now: datetime, minutes: int, tz: ZoneInfo, days: int = SEARCH_DAYS):
    """Half-hour-aligned slots in working hours on weekdays, starting at least 1 hour from now."""
    start = (now.astimezone(tz) + timedelta(hours=1)).replace(second=0, microsecond=0)
    start += timedelta(minutes=(-start.minute) % 30)
    t = start
    while t < start + timedelta(days=days):
        end = t + timedelta(minutes=minutes)
        if t.weekday() < 5 and WORKDAY[0] <= t.time() and end.time() <= WORKDAY[1] and end.date() == t.date():
            yield t, end
        t += timedelta(minutes=30)


def first_free(busy: list[list[tuple[datetime, datetime]]], slots, skip: int = 0):
    """The (skip+1)-th slot that overlaps nobody's busy blocks, or None."""
    for s, e in slots:
        if all(not (s < b_end and b_start < e) for person in busy for b_start, b_end in person):
            if skip == 0:
                return s, e
            skip -= 1
    return None


def _busy(person_id: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    res = google.service(person_id, "calendar", "v3").freebusy().query(body={
        "timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": "primary"}]}).execute()
    return [(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
            for b in res["calendars"]["primary"].get("busy", [])]


async def busy_until(person_id: str, max_hold_hours: int = 4) -> datetime | None:
    """If this person's calendar is busy right now, when the busy stretch ends (capped); else None."""
    tz = ZoneInfo(settings.DEMO_TIMEZONE)
    now = datetime.now(tz)
    blocks = sorted(await asyncio.to_thread(_busy, person_id, now, now + timedelta(hours=max_hold_hours)))
    end = None
    for s, e in blocks:
        if s <= (end or now) + timedelta(minutes=1):
            end = max(end or now, e)
    return min(end, now + timedelta(hours=max_hold_hours)).astimezone(tz) if end and end > now else None


async def find_common_slot(person_ids: list[str], skip: int = 0) -> tuple[datetime, datetime] | None:
    tz = ZoneInfo(settings.DEMO_TIMEZONE)
    now = datetime.now(tz)
    horizon = now + timedelta(days=SEARCH_DAYS + 1)
    busy = await asyncio.gather(*(asyncio.to_thread(_busy, p, now, horizon) for p in person_ids))
    return first_free(list(busy), candidate_slots(now, settings.MEETING_MINUTES, tz), skip)


async def book(organizer_id: str, guest_id: str, start: datetime, end: datetime, title: str, description: str) -> dict:
    """Create the event on the organizer's calendar, invite the guest, add a Meet link.
    Returns the Google event (htmlLink, hangoutLink, ...)."""
    def create():
        event = {
            "summary": title, "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": settings.DEMO_TIMEZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": settings.DEMO_TIMEZONE},
            "attendees": [{"email": google.account_email(guest_id)}],
            "conferenceData": {"createRequest": {"requestId": f"musketeer-{start.timestamp():.0f}",
                                                 "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
        }
        return google.service(organizer_id, "calendar", "v3").events().insert(
            calendarId="primary", body=event, sendUpdates="all", conferenceDataVersion=1).execute()
    return await asyncio.to_thread(create)
