"""'did' activity events -> expertise_events (spec §7.1, prompt P12).

Batch (pipeline): P12 for every unextracted event, then canonicalize all labels at once.
Live (one new event): the same steps for that event, with incremental canonicalization.
"""
import asyncio
import logging

from app.ai.muse import muse_json
from app.config import settings
from app.connectors.base import insert_event
from app.db import fetch_all, pool
from app.expertise import decay
from app.expertise.canonicalize import canonicalize, normalize_label
from app.schemas import EvidenceItem, ExtractExpertiseOut, NormalizedEvent

log = logging.getLogger("musketeer.extract")

WEIGHTS = {"built": 1.0, "solved": 1.2, "organized": 1.0, "reviewed": 0.5, "discussed": 0.3}
SNIPPET_MAX = 120

_EVENTS = """
    SELECT e.id, e.time, e.person_id, e.kind, e.title, e.text, e.metadata,
           p.name, p.title AS person_title, t.name AS team
    FROM activity_events e JOIN people p ON p.id = e.person_id JOIN teams t ON t.id = p.team_id
    WHERE e.direction = 'did' AND NOT e.expertise_extracted"""


async def extract_all() -> int:
    """Pipeline step 1. Returns the number of evidence rows written. Events whose Muse call fails
    stay unextracted and are retried on the next run."""
    events = await fetch_all(_EVENTS + " ORDER BY e.id")
    results = await asyncio.gather(*(_extract(ev) for ev in events), return_exceptions=True)
    done = []
    for ev, res in zip(events, results):
        if isinstance(res, Exception):
            log.warning("extraction failed for event %s: %s", ev["id"], res)
        else:
            done.append((ev, res))
    mapping = await canonicalize([i.label for _, items in done for i in items])
    written = 0
    for ev, items in done:
        written += await _store(ev, items, mapping)
    log.info("extracted %d events -> %d evidence rows (%d failed)", len(done), written, len(events) - len(done))
    return written


async def process_event(event_id: int) -> list[dict]:
    """Live extraction for one new 'did' event. Returns the evidence rows written."""
    rows = await fetch_all(_EVENTS + " AND e.id = %s", (event_id,))
    if not rows:
        return []
    ev = rows[0]
    items = await _extract(ev)
    await _store(ev, items, await canonicalize([i.label for i in items]))
    return await fetch_all("""SELECT x.skill_id, s.name AS skill_name, x.evidence_kind, x.weight, x.confidence, x.snippet
                              FROM expertise_events x JOIN skills s ON s.id = x.skill_id
                              WHERE x.source = 'activity' AND x.source_event_id = %s""", (event_id,))


async def _extract(ev: dict) -> list[EvidenceItem]:
    out = await muse_json("extract_expertise", {
        "person_name": ev["name"], "person_title": ev["person_title"], "person_team": ev["team"],
        "kind": ev["kind"], "title": ev["title"] or "", "text": ev["text"], "details": _details(ev["metadata"])},
        ExtractExpertiseOut)
    return [i for i in out.items[:4]
            if i.confidence >= settings.MIN_EVIDENCE_CONFIDENCE and normalize_label(i.label)]


def _details(meta: dict) -> str:
    parts = []
    if meta.get("directories"):
        parts.append(f"Directories: {', '.join(meta['directories'])}")
    if meta.get("commit_types"):
        parts.append("Commits: " + ", ".join(f"{n} {t}" for t, n in meta["commit_types"].items()))
    if meta.get("languages"):
        parts.append(f"Languages: {', '.join(meta['languages'])}")
    return "\n".join(parts)


async def _store(ev: dict, items: list[EvidenceItem], mapping: dict[str, str]) -> int:
    """Insert evidence and mark the event extracted, atomically. One skill counts once per event."""
    seen = set()
    async with pool.connection() as conn:   # commits on exit, rolls back on error
        for i in items:
            sid = mapping[normalize_label(i.label)]
            if sid in seen:
                continue
            seen.add(sid)
            await conn.execute("""
                INSERT INTO expertise_events (time, person_id, skill_id, evidence_kind, weight, confidence,
                                              source, source_event_id, snippet)
                VALUES (%s, %s, %s, %s, %s, %s, 'activity', %s, %s)""",
                (ev["time"], ev["person_id"], sid, i.evidence_kind, WEIGHTS[i.evidence_kind],
                 min(1.0, i.confidence), ev["id"], i.snippet.strip()[:SNIPPET_MAX]))
        await conn.execute("UPDATE activity_events SET expertise_extracted = true WHERE id = %s AND time = %s",
                           (ev["id"], ev["time"]))
    return len(seen)


async def ingest_did(ev: NormalizedEvent) -> list[dict] | None:
    """Store a live 'did' event and extract its evidence. None if it was a duplicate."""
    event_id = await insert_event(ev)
    if event_id is None:
        return None
    evidence = await process_event(event_id)
    await decay.recompute_person(ev.person_id)
    return evidence
