"""Muse generates the demo company's fixtures from seed/seed_spec.yaml (spec §9.2).

    python -m scripts.seed_generate [--regenerate]

Writes seed/fixtures/{roster,github,activity}.json (format: see scripts/seed_load.py).
Reuses committed fixtures unless --regenerate. All Muse calls are disk-cached.
"""
import argparse
import asyncio
import json
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from app.ai.muse import muse_json
from app.db import run_async
from app.schemas import NormalizedEvent

BACKEND = Path(__file__).resolve().parent.parent
SPEC = BACKEND / "seed" / "seed_spec.yaml"
OUT = BACKEND / "seed" / "fixtures"
EMAIL_DOMAIN = "musketeer-demo.com"
MAX_ATTEMPTS = 3

TEAM_REPO = {"t_payments": "payments", "t_messaging": "messaging", "t_ads": "ads", "t_devplat": "platform",
             "t_growth": "web", "t_design": "web"}

# Planted people who aren't identified by a code directory are checked by keywords instead.
PLANTED_KEYWORDS = {("recruiting_mlh", 0): ["hackathon"], ("amber_case", 0): ["referral"]}


# --- Muse output schemas ---

class RosterPerson(BaseModel):
    slot_id: str
    name: str
    title: str


class RosterOut(BaseModel):
    people: list[RosterPerson]


class GenPR(BaseModel):
    title: str
    body: str
    files: list[str]
    commits: list[str]
    days_ago: int


class GenSlack(BaseModel):
    text: str
    days_ago: int


class GenEmail(BaseModel):
    subject: str
    body: str
    days_ago: int


class ActivityOut(BaseModel):
    # Required, no extra keys: Muse occasionally mangles a key (seen: "emails euro"), which must
    # fail validation and retry rather than silently become an empty default.
    model_config = ConfigDict(extra="forbid")
    prs: list[GenPR]
    slack_messages: list[GenSlack]
    emails: list[GenEmail]


class SeedError(Exception):
    pass


# --- Roster ---

def build_slots(spec: dict) -> list[dict]:
    """Deterministic structure: ids in spec order, lead = slot 0, planted people placed on matching slots."""
    slots, n = [], 0
    for org in spec["orgs"]:
        for t in org["teams"]:
            first_engineer = 0 if t["engineers"] == t["size"] else t["size"] - t["engineers"]
            for i in range(t["size"]):
                n += 1
                slots.append({"slot_id": f"p_{n:03d}", "team_id": t["id"], "team": t["name"], "org_id": org["id"],
                              "org": org["name"], "is_lead": i == 0, "is_engineer": i >= first_engineer,
                              "planted": None, "fixed_name": None, "persona": None})

    all_dirs = [d for dirs in spec["repos"].values() for d in dirs]
    for overlap in spec["planted_overlaps"]:
        for idx, p in enumerate(overlap["people"]):
            needs_code = any(d in p["persona"] for d in all_dirs)
            slot = next(s for s in slots if s["team_id"] == p["team"] and not s["is_lead"]
                        and s["planted"] is None and s["is_engineer"] == needs_code)
            slot["planted"] = (overlap["id"], idx)
            slot["persona"] = p["persona"]
            if m := re.match(r"^([A-Z][a-z]+ [A-Z][a-z]+)[,;]", p["persona"]):
                slot["fixed_name"] = m.group(1)
            if detail := spec.get("planted_details", {}).get(overlap["id"], [None] * 2)[idx]:
                slot["persona"] += f". {detail}"

    # Everyone else gets a distinct focus and stays off the planted topics, so planted pairs stand out.
    off_limits = "; ".join(p["persona"] for o in spec["planted_overlaps"] for p in o["people"])
    for team_id, focuses in spec.get("member_focus", {}).items():
        free = [s for s in slots if s["team_id"] == team_id and s["planted"] is None]
        for s, focus in zip(free, focuses):
            s["persona"] = (f"{focus}. Stay on this area. Do not work on any of these, which belong to "
                            f"colleagues: {off_limits}")
    return slots


async def generate_roster(spec: dict, slots: list[dict]) -> dict[str, RosterPerson]:
    listing = "\n".join(
        f"- {s['slot_id']}: {s['team']} ({s['org']}), {'LEAD' if s['is_lead'] else 'member'}, "
        f"{'engineer' if s['is_engineer'] else 'non-engineer'}"
        + (f", name must be {s['fixed_name']}" if s["fixed_name"] else "")
        + (f", persona: {s['persona']}" if s["persona"] else "")
        for s in slots)
    for attempt in range(MAX_ATTEMPTS):
        out = await muse_json("seed_roster", {"company": spec["company"], "slots": listing
                                              + ("\n" * attempt)}, RosterOut)  # attempt varies the cache key
        by_slot = {p.slot_id: p for p in out.people}
        names = [p.name for p in out.people]
        fixed_ok = all(by_slot.get(s["slot_id"]) and by_slot[s["slot_id"]].name == s["fixed_name"]
                       for s in slots if s["fixed_name"])
        if set(by_slot) == {s["slot_id"] for s in slots} and len(set(names)) == len(names) and fixed_ok:
            return by_slot
        print(f"  roster attempt {attempt + 1} invalid; retrying", file=sys.stderr)
    raise SeedError("Muse could not produce a valid roster")


def assemble_roster(spec: dict, slots: list[dict], people: dict[str, RosterPerson], generated_at: datetime) -> dict:
    lead_of = {s["team_id"]: s["slot_id"] for s in slots if s["is_lead"]}
    org_head = {}
    for org in spec["orgs"]:
        org_head[org["id"]] = lead_of[org["teams"][0]["id"]]  # first lead listed per org

    def email(name: str) -> str:
        return ".".join(re.sub(r"[^a-z]", "", part.lower()) for part in name.split()) + f"@{EMAIL_DOMAIN}"

    roster_people = []
    for s in slots:
        pid = s["slot_id"]
        manager = (None if pid == org_head[s["org_id"]] else org_head[s["org_id"]]) if s["is_lead"] else lead_of[s["team_id"]]
        roster_people.append({"id": pid, "team_id": s["team_id"], "manager_id": manager, "name": people[pid].name,
                              "title": people[pid].title, "email": email(people[pid].name),
                              "is_engineer": s["is_engineer"], "is_manager": s["is_lead"]})

    planted: dict[str, list[str]] = {}
    for s in slots:
        if s["planted"]:
            planted.setdefault(s["planted"][0], [None] * 2)[s["planted"][1]] = s["slot_id"]
    by_name = {p["name"]: p["id"] for p in roster_people}
    demo = spec["demo"]
    amber_person = planted["amber_case"][0]
    amber_persona = next(s["persona"] for s in slots if s["slot_id"] == amber_person)
    # The task is the first sentence; planted_details appends more after it.
    summary = re.search(r"open task[^:]*:\s*([^.]+)", amber_persona).group(1).strip()
    return {
        "generated_at": generated_at.isoformat(),
        "orgs": [{"id": o["id"], "name": o["name"]} for o in spec["orgs"]],
        "teams": [{"id": t["id"], "org_id": o["id"], "name": t["name"], "lead_id": lead_of[t["id"]]}
                  for o in spec["orgs"] for t in o["teams"]],
        "people": roster_people,
        "planted": planted,
        "amber_task": {"person_id": amber_person, "summary": summary[:1].upper() + summary[1:],
                       "task_type": "project", "source": "email"},
        "demo": {"viewer_id": lead_of[demo["viewer"]["team"]], "email_recipient_id": by_name[demo["email_recipient"]]},
    }


# --- Activity ---

def allowed_dirs(spec: dict, slot: dict) -> list[str]:
    """Own repo's directories, plus any directory the persona names (e.g. shared/kafka-client)."""
    own = spec["repos"][TEAM_REPO[slot["team_id"]]]
    persona = slot["persona"] if slot["planted"] else (slot["persona"] or "").split(" Do not work on")[0]
    named = [d for dirs in spec["repos"].values() for d in dirs if d in persona]
    return list(dict.fromkeys(own + named))


def check_planted(slot: dict, act: ActivityOut, spec: dict) -> str | None:
    """Returns a problem description, or None if the planted overlap shows up in the activity."""
    if not slot["planted"]:
        return None
    all_dirs = [d for dirs in spec["repos"].values() for d in dirs]
    mentioned = [d for d in all_dirs if d in slot["persona"]]
    # Most specific only: "services/ads/serving/throttle" also contains "services/ads/serving".
    needed_dirs = [d for d in mentioned if not any(o.startswith(d + "/") for o in mentioned)]
    touched = {f.rsplit("/", 1)[0] for pr in act.prs for f in pr.files}
    for d in needed_dirs:
        if d not in touched:
            return f"no PR touches {d}"
    words = PLANTED_KEYWORDS.get(slot["planted"], [])
    corpus = " ".join([m.text for m in act.slack_messages] + [e.subject + " " + e.body for e in act.emails]
                      + [pr.title + " " + pr.body for pr in act.prs]).lower()
    missing = [w for w in words if w not in corpus]
    return f"activity never mentions {missing}" if missing else None


async def generate_activity(spec: dict, slot: dict, roster: dict) -> ActivityOut:
    person = next(p for p in roster["people"] if p["id"] == slot["slot_id"])
    names = {p["id"]: p["name"] for p in roster["people"]}
    teammates = [p["name"] for p in roster["people"] if p["team_id"] == slot["team_id"] and p["id"] != person["id"]]
    ranges = spec["activity_per_person"]["engineer" if slot["is_engineer"] else "non_engineer"]
    wanted = {k: v for k, v in ranges.items() if k != "reviews"}  # reviews come from teammates' PRs, not Muse
    counts = "\n".join(f"- {kind.replace('_', ' ')}: between {lo} and {hi}"
                       + (" (required, do not skip)" if kind == "emails" else "") for kind, (lo, hi) in wanted.items())
    persona = slot["persona"] or f"Typical, specific work for someone on {slot['team']}."

    for attempt in range(MAX_ATTEMPTS):
        act = await muse_json("seed_activity", {
            "company": spec["company"], "name": person["name"], "title": person["title"], "team": slot["team"],
            "org": slot["org"], "manager": names.get(person["manager_id"], "none"), "teammates": ", ".join(teammates),
            "persona": persona + ("\n" * attempt), "history_days": spec["history_days"] - 2, "counts": counts,
            "directories": ", ".join(allowed_dirs(spec, slot)) if slot["is_engineer"] else "(not an engineer)",
        }, ActivityOut)
        short = [k for k, (lo, _) in wanted.items() if len(getattr(act, k)) < lo]
        problem = f"too few {short}" if short else check_planted(slot, act, spec)
        if problem is None:
            return act
        print(f"  {person['id']} attempt {attempt + 1}: {problem}; retrying", file=sys.stderr)
    raise SeedError(f"{person['id']} ({slot['planted']}): planted overlap never appeared")


def to_fixtures(spec: dict, slots: list[dict], roster: dict, acts: dict[str, ActivityOut],
                generated_at: datetime) -> tuple[list[dict], list[dict]]:
    rng = random.Random(7)
    dirs_repo = {d: repo for repo, dirs in spec["repos"].items() for d in dirs}
    allowed = {s["slot_id"]: allowed_dirs(spec, s) for s in slots if s["is_engineer"]}
    at = lambda days_ago: generated_at - timedelta(days=max(1, days_ago), hours=rng.uniform(0, 8))

    prs, numbers = [], {repo: 100 for repo in spec["repos"]}
    for pid, act in acts.items():
        for pr in act.prs:
            # Keep only files in this person's allowed directories, and only the first file's repo.
            files = [f for f in pr.files if f.rsplit("/", 1)[0] in allowed.get(pid, [])]
            if not files:
                continue
            repo = dirs_repo[files[0].rsplit("/", 1)[0]]
            files = [f for f in files if dirs_repo[f.rsplit("/", 1)[0]] == repo]
            numbers[repo] += 1
            created = at(pr.days_ago)
            prs.append({"repo": repo, "number": numbers[repo], "title": pr.title, "body": pr.body[:400],
                        "author": pid, "created_at": created.isoformat(),
                        "merged_at": (created + timedelta(hours=rng.uniform(2, 48))).isoformat(),
                        "files": files, "commits": [{"message": c} for c in pr.commits[:6]], "reviews": []})

    # Reviews: each engineer reviews 3-6 teammates' PRs (same team only).
    team_of = {p["id"]: p["team_id"] for p in roster["people"]}
    lo, hi = spec["activity_per_person"]["engineer"]["reviews"]
    for pid in sorted(allowed):
        pool = [pr for pr in prs if pr["author"] != pid and team_of[pr["author"]] == team_of[pid]]
        for pr in rng.sample(pool, min(len(pool), rng.randint(lo, hi))):
            created = datetime.fromisoformat(pr["created_at"])
            pr["reviews"].append({"reviewer": pid, "body": "Looks good.",
                                  "submitted_at": (created + timedelta(hours=rng.uniform(0.5, 2))).isoformat()})

    activity = []
    for pid, act in acts.items():
        for i, m in enumerate(act.slack_messages):
            activity.append(NormalizedEvent(source="slack", kind="slack_message", external_id=f"seed-slack-{pid}-{i}",
                                            time=at(m.days_ago), person_id=pid, direction="did", title=None,
                                            text=m.text[:800], metadata={}).model_dump(mode="json"))
        for i, e in enumerate(act.emails):
            activity.append(NormalizedEvent(source="email", kind="email", external_id=f"seed-email-{pid}-{i}",
                                            time=at(e.days_ago), person_id=pid, direction="did", title=e.subject,
                                            text=f"{e.subject}\n{e.body}"[:800], metadata={}).model_dump(mode="json"))
    return prs, activity


async def main(regenerate: bool) -> None:
    if (OUT / "roster.json").exists() and not regenerate:
        print(f"Fixtures already exist in {OUT}; pass --regenerate to rebuild.")
        return
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    slots = build_slots(spec)

    print("Generating roster…")
    roster = assemble_roster(spec, slots, await generate_roster(spec, slots), generated_at)

    print(f"Generating activity for {len(slots)} people (4 at a time)…")
    done = 0

    async def one(slot: dict) -> tuple[str, ActivityOut]:
        nonlocal done
        act = await generate_activity(spec, slot, roster)
        done += 1
        print(f"  [{done:>2}/{len(slots)}] {slot['slot_id']} {len(act.prs)} PRs, "
              f"{len(act.slack_messages)} Slack, {len(act.emails)} emails")
        return slot["slot_id"], act

    acts = dict(await asyncio.gather(*(one(s) for s in slots)))
    prs, activity = to_fixtures(spec, slots, roster, acts, generated_at)

    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in (("roster", roster), ("github", prs), ("activity", activity)):
        (OUT / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    names = {p["id"]: p["name"] for p in roster["people"]}
    print(f"\nWrote {len(roster['people'])} people, {len(prs)} PRs, "
          f"{sum(len(p['reviews']) for p in prs)} reviews, {len(activity)} Slack/email events to {OUT}")
    for k, ids in roster["planted"].items():
        print(f"  planted {k:<15} {' <-> '.join(f'{i} {names[i]}' for i in ids)}")
    print(f"  viewer {roster['demo']['viewer_id']} {names[roster['demo']['viewer_id']]}, "
          f"email recipient {roster['demo']['email_recipient_id']} {names[roster['demo']['email_recipient_id']]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--regenerate", action="store_true")
    run_async(main(ap.parse_args().regenerate))
