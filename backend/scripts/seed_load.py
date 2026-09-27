"""Load seed fixtures into the DB (spec §9.3).

    python -m scripts.seed_load [--reset] [--fixtures seed/fixtures]

Fixture files (written by seed_generate.py):
  roster.json   {generated_at, orgs, teams, people, planted: {overlap_id: [pid, pid]},
                 amber_task: {person_id, summary, task_type, source}, demo: {viewer_id, email_recipient_id}}
  github.json   [GitHub-shaped PR, spec §6.1]
  github_temporal.json  optional, same shape: hand-written planted_temporal PRs (stale expert, emerging skill)
  activity.json [NormalizedEvent dict]  (non-GitHub 'did' events: Slack posts, sent emails)
All fixture timestamps are shifted by (now - generated_at) so history always ends today.
"""
import argparse
import asyncio
import itertools
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from app.connectors.base import insert_event
from app.connectors.github import GitHubConnector
from app.db import apply_schema, execute, fetch_one, pool, run_async
from app.schemas import NormalizedEvent

BACKEND = Path(__file__).resolve().parent.parent
SEED_SPEC = BACKEND / "seed" / "seed_spec.yaml"
SLACK_MAP = BACKEND / "seed" / "slack_map.json"


async def load_roster(roster: dict) -> None:
    for o in roster["orgs"]:
        await execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (o["id"], o["name"]))
    for t in roster["teams"]:
        await execute("INSERT INTO teams (id, org_id, name) VALUES (%s, %s, %s)", (t["id"], t["org_id"], t["name"]))
    for p in roster["people"]:
        await execute("""INSERT INTO people (id, team_id, name, title, email, is_engineer, is_manager)
                         VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                      (p["id"], p["team_id"], p["name"], p["title"], p["email"], p["is_engineer"], p["is_manager"]))
    # Managers and leads reference people, so set them once everyone exists.
    for p in roster["people"]:
        if p.get("manager_id"):
            await execute("UPDATE people SET manager_id = %s WHERE id = %s", (p["manager_id"], p["id"]))
    for t in roster["teams"]:
        await execute("UPDATE teams SET lead_id = %s WHERE id = %s", (t["lead_id"], t["id"]))


async def load_slack_map(roster: dict) -> int:
    if not SLACK_MAP.exists():
        return 0
    by_name = {p["name"]: p["id"] for p in roster["people"]}
    mapping = json.loads(SLACK_MAP.read_text(encoding="utf-8"))
    for name, slack_id in mapping.items():
        await execute("UPDATE people SET slack_user_id = %s WHERE id = %s", (slack_id, by_name[name]))
    return len(mapping)


async def load_events(fixtures: Path, shift: timedelta) -> int:
    events: list[NormalizedEvent] = []
    for name in ("github.json", "github_temporal.json"):   # github_temporal: hand-written planted_temporal PRs
        path = fixtures / name
        for pr in json.loads(path.read_text(encoding="utf-8")) if path.exists() else []:
            events += GitHubConnector().normalize(pr)
    events += [NormalizedEvent(**e) for e in json.loads((fixtures / "activity.json").read_text(encoding="utf-8"))]
    for ev in events:
        ev.time += shift
    ids = await asyncio.gather(*(insert_event(ev) for ev in events))
    return sum(1 for i in ids if i is not None)


async def create_amber_task(amber: dict) -> None:
    await execute("""INSERT INTO tasks (person_id, source, summary, task_type, status, created_at)
                     VALUES (%s, %s, %s, %s, 'open', now() - INTERVAL '2 days')""",
                  (amber["person_id"], amber.get("source", "email"), amber["summary"], amber.get("task_type")))


def pick_history_pairs(roster: dict, spec: dict, rng: random.Random) -> list[tuple[str, str]]:
    """~3 cross-org, ~8 cross-team (same org), rest within-team. Never planted pairs or bridge teams."""
    team_of = {p["id"]: p["team_id"] for p in roster["people"]}
    org_of = {t["id"]: t["org_id"] for t in roster["teams"]}
    banned = {frozenset(pair) for ids in roster["planted"].values() for pair in itertools.combinations(ids, 2)}
    bridge_teams = [set(o["team_bridge"]) for o in spec["planted_overlaps"] if o.get("team_bridge")]

    pools = {"cross_org": [], "cross_team": [], "within": []}
    for a, b in itertools.combinations(sorted(team_of), 2):
        ta, tb = team_of[a], team_of[b]
        if frozenset((a, b)) in banned or any({ta, tb} == bt for bt in bridge_teams):
            continue
        kind = "within" if ta == tb else "cross_team" if org_of[ta] == org_of[tb] else "cross_org"
        pools[kind].append((a, b))

    total = spec["seed_history"]["connections"]
    picked = rng.sample(pools["cross_org"], 3) + rng.sample(pools["cross_team"], 8)
    picked += rng.sample(pools["within"], total - len(picked))
    return [pair if rng.random() < 0.5 else pair[::-1] for pair in picked]


async def create_seed_history(roster: dict, spec: dict, rng: random.Random) -> int:
    hist = spec["seed_history"]
    statuses = [s for s, n in hist["statuses"].items() for _ in range(n)]
    rng.shuffle(statuses)
    now = datetime.now(timezone.utc)
    minutes = lambda lo, hi: timedelta(minutes=rng.uniform(lo, hi))

    for (requester, helper), status in zip(pick_history_pairs(roster, spec, rng), statuses):
        created = now - timedelta(days=rng.uniform(1, spec["history_days"] * 0.7))
        requested = created + minutes(1, 5)
        task_status = "connected" if status in ("accepted", "active") else "dismissed"
        task = await fetch_one("""INSERT INTO tasks (person_id, source, summary, task_type, status, created_at)
                                  VALUES (%s, 'slack', 'Earlier task (seed history)', 'other', %s, %s) RETURNING id""",
                               (requester, task_status, created - minutes(5, 60)))
        events = [(created, "suggested", None, None), (requested, "requested", requester, None)]
        row = {"accepted_at": None, "active_at": None, "closed_at": None, "message_count": 0,
               "helpful_requester": None, "helpful_helper": None, "feedback_requested_at": None}

        if status in ("accepted", "active"):
            row["accepted_at"] = requested + minutes(3, 90)
            events.append((row["accepted_at"], "accepted", helper, None))
        if status == "active":
            row["active_at"] = row["accepted_at"] + minutes(5, 120)
            events.append((row["active_at"], "active", None, None))
            row["message_count"] = rng.randint(4, 20)
            span = (min(now, row["active_at"] + timedelta(days=1)) - row["accepted_at"]).total_seconds()
            for i in range(row["message_count"]):
                t = row["accepted_at"] + timedelta(seconds=rng.uniform(0, span))
                events.append((t, "message", requester if i % 2 == 0 else helper, None))
            row["feedback_requested_at"] = row["active_at"] + minutes(3, 5)
            for who, col in ((requester, "helpful_requester"), (helper, "helpful_helper")):
                row[col] = rng.random() < hist["helpful_rate"]
                events.append((row["feedback_requested_at"] + minutes(1, 30), "feedback", who, row[col]))
        if status == "declined":
            row["closed_at"] = requested + minutes(10, 60)
            events.append((row["closed_at"], "declined", helper, None))
        if status == "expired":
            row["closed_at"] = min(now, created + timedelta(days=2))
            events.append((row["closed_at"], "expired", None, None))

        conn = await fetch_one("""
            INSERT INTO connections (requester_id, helper_id, task_id, origin, reason, match_score, status,
                message_count, helpful_requester, helpful_helper, feedback_requested_at, is_seed,
                created_at, accepted_at, active_at, closed_at)
            VALUES (%s, %s, %s, 'auto', 'Matched on overlapping recent work.', %s, %s,
                %s, %s, %s, %s, true, %s, %s, %s, %s) RETURNING id""",
            (requester, helper, task["id"], round(rng.uniform(0.72, 0.95), 2), status,
             row["message_count"], row["helpful_requester"], row["helpful_helper"], row["feedback_requested_at"],
             created, row["accepted_at"], row["active_at"], row["closed_at"]))
        for t, event, person, value in events:
            await execute("""INSERT INTO connection_events (time, connection_id, event, person_id, value)
                             VALUES (%s, %s, %s, %s, %s)""", (min(t, now), conn["id"], event, person, value))
    return len(statuses)


async def create_pending_intros(spec: dict) -> int:
    """Intros that never resolve (seed_spec.pending_intros), so there is always something in progress."""
    made = 0
    for requester, helper in spec.get("pending_intros", []):
        if await fetch_one("""SELECT 1 FROM connections WHERE LEAST(requester_id, helper_id) = LEAST(%(a)s, %(b)s)
                              AND GREATEST(requester_id, helper_id) = GREATEST(%(a)s, %(b)s)""", {"a": requester, "b": helper}):
            continue
        row = await fetch_one("""
            INSERT INTO connections (requester_id, helper_id, origin, reason, match_score, status, is_seed, created_at)
            VALUES (%s, %s, 'auto', 'Similar recent work; intro sent, waiting on a reply.', 0.7, 'requested', true,
                    now() - INTERVAL '8 days') RETURNING id, created_at""", (requester, helper))
        for event, minutes, who in (("suggested", 0, None), ("requested", 3, requester)):
            await execute("""INSERT INTO connection_events (time, connection_id, event, person_id)
                             VALUES (%s + make_interval(mins => %s), %s, %s, %s)""",
                          (row["created_at"], minutes, row["id"], event, who))
        made += 1
    return made


async def load(fixtures: Path, reset: bool, allow_public_reset: bool = False) -> dict:
    roster = json.loads((fixtures / "roster.json").read_text(encoding="utf-8"))
    spec = yaml.safe_load(SEED_SPEC.read_text(encoding="utf-8"))
    shift = datetime.now(timezone.utc) - datetime.fromisoformat(roster["generated_at"])

    if reset:
        await apply_schema(reset=True, allow_public_reset=allow_public_reset)
    await load_roster(roster)
    mapped = await load_slack_map(roster)
    n_events = await load_events(fixtures, shift)
    await create_amber_task(roster["amber_task"])
    n_conn = await create_seed_history(roster, spec, random.Random(42))
    await create_pending_intros(spec)
    return {"people": len(roster["people"]), "events": n_events, "connections": n_conn,
            "slack_mapped": mapped, **roster.get("demo", {})}


async def main(fixtures: Path, reset: bool) -> None:
    await pool.open()
    try:
        summary = await load(fixtures, reset, allow_public_reset=True)  # the one place allowed to wipe public
    finally:
        await pool.close()
    for k, v in summary.items():
        print(f"{k:>20}: {v}")
    print("Set DEFAULT_VIEWER_ID / DEMO_EMAIL_FALLBACK_PERSON in .env from viewer_id / email_recipient_id above.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="drop and recreate all tables first")
    ap.add_argument("--fixtures", type=Path, default=BACKEND / "seed" / "fixtures")
    args = ap.parse_args()
    run_async(main(args.fixtures, args.reset))
