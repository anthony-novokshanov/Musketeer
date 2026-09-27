"""MATCHER_MODE=hybrid with a stand-in embedder (bag of words hashed into 768 dims; no model download).
Real pgvector + StreamingDiskANN queries run against the test schema."""
import hashlib
import math
import re

import pytest

from app.ai import embedder
from app.db import execute, fetch_one
from app.matching.hybrid_matcher import HybridMatcher, nearest_people, nearest_work
from app.pipeline import embeddings

RARE = "sprocket calibration drift"


def fake_embed(texts):
    out = []
    for t in texts:
        v = [0.0] * embedder.DIMS
        for w in re.findall(r"[a-z]+", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % embedder.DIMS] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


@pytest.fixture
async def hybrid(seeded, fake_muse, monkeypatch):
    async def embed(texts):
        return fake_embed(texts)
    monkeypatch.setattr(embedder, "embed", embed)
    # Someone outside the planted pairs did RARE work that no skill in the graph describes.
    expert = seeded["planted"]["rate_limit"][1]
    await execute("""INSERT INTO activity_events (time, person_id, direction, source, kind, external_id, title, text)
                     VALUES (now() - INTERVAL '3 days', %s, 'did', 'slack', 'slack_message', 'hybrid-test', NULL, %s)""",
                  (expert, f"Fixed the {RARE} in the build farm"))
    await embeddings.embed_all()
    yield expert
    await execute("DELETE FROM activity_events WHERE external_id = 'hybrid-test'")


async def test_embed_all_fills_every_person_and_work_item(hybrid):
    row = await fetch_one("""SELECT (SELECT count(*) FROM people WHERE summary_embedding IS NULL) AS people,
                                    (SELECT count(*) FROM activity_events WHERE direction = 'did' AND embedding IS NULL) AS events""")
    assert (row["people"], row["events"]) == (0, 0)


async def test_nearest_work_finds_wording_the_skill_graph_missed(hybrid):
    [vec] = fake_embed([f"who can help with {RARE}?"])
    assert (await nearest_work(vec, set(), 3))[0][0] == hybrid
    assert hybrid not in [p for p, _ in await nearest_work(vec, {hybrid}, 3)]       # exclusions hold
    [me] = fake_embed(["Does concrete work."])
    assert len(await nearest_people(me, set(), 5)) == 5


async def test_task_candidates_add_retrieval_to_the_temporal_pool(hybrid, fake_muse):
    requester = next(p for p in (await fetch_one("SELECT array_agg(id ORDER BY id) AS ids FROM people"))["ids"]
                     if p != hybrid)
    task = await fetch_one("""INSERT INTO tasks (person_id, source, summary, task_type, required_skills)
                              VALUES (%s, 'email', %s, 'project', '[]'::jsonb) RETURNING id""", (requester, f"Fix {RARE}"))
    seen = []
    fake_muse.handler = lambda prompt, schema: seen.append(prompt) or {"candidates": [
        {"person_id": hybrid, "score": 90, "reason": f"Fixed {RARE} 3 days ago."}]}
    try:
        matches = await HybridMatcher().rank_for_task(task["id"])
        assert matches[0].person_id == hybrid
        assert f"[{hybrid}]" in seen[0]
        assert (await fetch_one("SELECT embedding IS NOT NULL AS v FROM tasks WHERE id = %s", (task["id"],)))["v"]
    finally:
        await execute("DELETE FROM tasks WHERE id = %s", (task["id"],))


async def test_person_ranking_sends_muse_a_shortlist_not_the_roster(hybrid, fake_muse, seeded):
    target = seeded["planted"]["kafka_lag"][0]
    calls = []
    fake_muse.handler = lambda prompt, schema: calls.append(prompt) or {"matches": []}
    await HybridMatcher().rank_for_person(target)
    prefix = fake_muse.calls[0][1][0]["content"]
    shortlisted = set(re.findall(r"^\[(p_\d+)\]", prefix, flags=re.MULTILINE))
    assert target in shortlisted and 1 < len(shortlisted) < 40
    assert "profile similarity" in calls[0]            # Contriever neighbours are listed for Muse
