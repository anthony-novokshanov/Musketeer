"""Hybrid mode: Contriever embeddings for people and their work, stored in pgvector (spec §8.6).

People are embedded from their profile (title, summary, focus areas); 'did' work items from their text.
Both columns carry StreamingDiskANN indexes (pgvectorscale), so retrieval stays fast as the company grows.
"""
import logging

from app.ai import embedder
from app.db import fetch_all, pool

log = logging.getLogger("musketeer.embeddings")

CHUNK = 256   # rows embedded and written per round


def person_text(p: dict) -> str:
    focus = ", ".join(p["focus_areas"] or [])
    return f"{p['title']}. {p['summary'] or ''}" + (f" Focus: {focus}." if focus else "")


def event_text(e: dict) -> str:
    return f"{e['title']}. {e['text']}" if e["title"] else e["text"]


async def _write(sql: str, rows: list[tuple]) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.executemany(sql, rows)


async def embed_people(only_missing: bool = False) -> int:
    people = await fetch_all(f"""SELECT id, title, summary, focus_areas FROM people
                                 {'WHERE summary_embedding IS NULL' if only_missing else ''} ORDER BY id""")
    vecs = await embedder.embed([person_text(p) for p in people])
    await _write("UPDATE people SET summary_embedding = %s::vector WHERE id = %s",
                 [(embedder.literal(v), p["id"]) for p, v in zip(people, vecs)])
    return len(people)


async def embed_missing_events() -> int:
    """Embed 'did' work items that have no embedding yet (new live events, or a first run)."""
    total = 0
    while True:
        events = await fetch_all("""SELECT id, time, title, text FROM activity_events
                                    WHERE direction = 'did' AND embedding IS NULL ORDER BY time LIMIT %s""", (CHUNK,))
        if not events:
            return total
        vecs = await embedder.embed([event_text(e) for e in events])
        await _write("UPDATE activity_events SET embedding = %s::vector WHERE id = %s AND time = %s",
                     [(embedder.literal(v), e["id"], e["time"]) for e, v in zip(events, vecs)])
        total += len(events)


async def embed_all() -> None:
    people = await embed_people()
    events = await embed_missing_events()
    log.info("embedded %d people and %d work items", people, events)
