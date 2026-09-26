from typing import Protocol

from psycopg.types.json import Jsonb

from app.db import fetch_one
from app.schemas import NormalizedEvent


class Connector(Protocol):
    """Converts raw source data into NormalizedEvents. Nothing downstream knows the source."""

    def normalize(self, raw: dict) -> list[NormalizedEvent]: ...


async def insert_event(ev: NormalizedEvent) -> int | None:
    """Insert into activity_events; returns the new id, or None if it was a duplicate."""
    row = await fetch_one("""
        INSERT INTO activity_events (time, person_id, direction, source, kind, external_id, title, text, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source, external_id, time) DO NOTHING
        RETURNING id""",
        (ev.time, ev.person_id, ev.direction, ev.source, ev.kind, ev.external_id, ev.title,
         ev.text[:800], Jsonb(ev.metadata)))
    return row["id"] if row else None
