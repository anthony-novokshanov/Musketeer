"""Raw skill labels -> canonical skills (spec §7.2, prompt P13).

The first run (no skills yet) builds the vocabulary in one batch call. After that every unmapped
label goes through the incremental call, so existing skill ids never change. skill_labels caches
every mapping, so a label costs at most one Muse call ever.
"""
import logging
import re
from collections import Counter

from app.ai.muse import muse_json
from app.db import execute, fetch_all
from app.schemas import CanonicalizeLabelOut, CanonicalizeSkillsOut

log = logging.getLogger("musketeer.canonicalize")

TARGET_SKILLS = (60, 120)
MUSE_EDGE_WEIGHT = 0.5


def normalize_label(label: str) -> str:
    return " ".join(label.lower().split())


def skill_id(name: str) -> str:
    """Deterministic id: 'kafka consumer lag' -> 'sk_kafka_consumer_lag'."""
    return "sk_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


async def canonicalize(labels: list[str]) -> dict[str, str]:
    """Map raw labels (normalized) to skill ids, creating skills as needed."""
    wanted = Counter(normalize_label(l) for l in labels)
    mapping = await _known(list(wanted))
    missing = [l for l in wanted if l not in mapping]
    if missing and not await fetch_all("SELECT 1 FROM skills LIMIT 1"):
        mapping |= await _batch({l: wanted[l] for l in missing})
        missing = [l for l in missing if l not in mapping]
    for label in missing:   # sequential: each call sees the skills the previous one created
        mapping[label] = await _one(label)
    return mapping


async def _known(labels: list[str]) -> dict[str, str]:
    rows = await fetch_all("SELECT raw_label, skill_id FROM skill_labels WHERE raw_label = ANY(%s)", (labels,))
    return {r["raw_label"]: r["skill_id"] for r in rows}


async def _batch(counts: dict[str, int]) -> dict[str, str]:
    lines = "\n".join(f"{label} ({n})" for label, n in sorted(counts.items()))
    out = await muse_json("canonicalize_skills", {"labels": lines, "target_min": TARGET_SKILLS[0],
                                                  "target_max": TARGET_SKILLS[1]}, CanonicalizeSkillsOut)
    ids_by_name, mapping = {}, {}
    for s in out.skills:
        name = normalize_label(s.name)
        sid = skill_id(name)
        if sid in ids_by_name.values():
            continue   # two names slugged to one id: keep the first
        await _insert_skill(sid, name, s.kind, s.description)
        ids_by_name[name] = sid
        for raw in map(normalize_label, s.raw_labels):
            if raw in counts and raw not in mapping:
                mapping[raw] = sid
    for label, sid in mapping.items():
        await _insert_label(label, sid)
    for s in out.skills:
        a = ids_by_name.get(normalize_label(s.name))
        for rel in s.related:
            b = ids_by_name.get(normalize_label(rel))
            if a and b and a != b:
                await execute("""INSERT INTO skill_edges (skill_a, skill_b, weight, origin) VALUES (%s, %s, %s, 'muse')
                                 ON CONFLICT DO NOTHING""", (min(a, b), max(a, b), MUSE_EDGE_WEIGHT))
    log.info("batch canonicalization: %d labels -> %d skills (%d labels left for incremental)",
             len(counts), len(ids_by_name), len(counts) - len(mapping))
    return mapping


async def _one(label: str) -> str:
    skills = await fetch_all("SELECT id, name, description FROM skills ORDER BY name")
    listing = "\n".join(f"{s['name']}: {s['description']}" for s in skills) or "(none yet)"
    out = await muse_json("canonicalize_label", {"label": label, "skills": listing}, CanonicalizeLabelOut)
    name = normalize_label(out.skill_name)
    existing = {s["name"]: s["id"] for s in skills}
    if name in existing:
        sid = existing[name]
    else:
        # is_new, or Muse named a skill that doesn't exist: create it.
        sid = skill_id(name)
        await _insert_skill(sid, name, out.kind or "problem", out.description or name)
    await _insert_label(label, sid)
    return sid


async def _insert_skill(sid: str, name: str, kind: str, description: str) -> None:
    await execute("""INSERT INTO skills (id, name, kind, description) VALUES (%s, %s, %s, %s)
                     ON CONFLICT DO NOTHING""", (sid, name, kind, description))


async def _insert_label(label: str, sid: str) -> None:
    await execute("INSERT INTO skill_labels (raw_label, skill_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                  (label, sid))
