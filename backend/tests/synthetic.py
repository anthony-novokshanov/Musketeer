"""Synthetic fixtures in the seed_generate output format, built from the real seed_spec.yaml
(same teams and counts, placeholder names), plus a deterministic fake Muse for the pipeline."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from scripts.seed_load import SEED_SPEC

GENERATED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


def build_fixtures(out: Path) -> dict:
    spec = yaml.safe_load(SEED_SPEC.read_text(encoding="utf-8"))
    orgs, teams, people, members = [], [], [], {}
    n = 0
    for org in spec["orgs"]:
        orgs.append({"id": org["id"], "name": org["name"]})
        org_head = None
        for t in org["teams"]:
            ids = [f"p_{n + i + 1:03d}" for i in range(t["size"])]
            n += t["size"]
            members[t["id"]] = ids
            lead = ids[0]
            org_head = org_head or lead
            teams.append({"id": t["id"], "org_id": org["id"], "name": t["name"], "lead_id": lead})
            for i, pid in enumerate(ids):
                people.append({"id": pid, "team_id": t["id"], "name": f"Person {pid}", "title": "Engineer" if i < t["engineers"] else "Specialist",
                               "email": f"{pid}@example.com", "is_engineer": i < t["engineers"], "is_manager": i == 0,
                               "manager_id": (None if pid == org_head else org_head) if i == 0 else lead})

    planted = {"recruiting_mlh": [members["t_uni"][1], members["t_events"][1]],
               "kafka_lag": [members["t_payments"][1], members["t_messaging"][1]],
               "rate_limit": [members["t_ads"][1], members["t_devplat"][1]],
               "amber_case": [members["t_events"][2], members["t_growth"][1]]}
    roster = {"generated_at": GENERATED_AT.isoformat(), "orgs": orgs, "teams": teams, "people": people,
              "planted": planted,
              "amber_task": {"person_id": planted["amber_case"][0], "source": "email", "task_type": "project",
                             "summary": "Build a candidate referral tracking dashboard"},
              "demo": {"viewer_id": members["t_events"][0], "email_recipient_id": planted["recruiting_mlh"][1]}}

    repo_of = {"t_payments": "payments", "t_messaging": "messaging", "t_ads": "ads", "t_devplat": "platform",
               "t_growth": "web", "t_design": "web"}
    team_of = {p["id"]: p["team_id"] for p in people}
    prs, number = [], 100
    for p in (p for p in people if p["is_engineer"]):
        repo = repo_of[team_of[p["id"]]]
        base_dirs = spec["repos"][repo]
        for k in range(3):
            number += 1
            files = [f"{base_dirs[k % len(base_dirs)]}/file{k}.py"]
            if p["id"] in (planted["kafka_lag"]):
                files.append("shared/kafka-client/consumer.py")
            when = GENERATED_AT - timedelta(days=5 + 10 * k)
            prs.append({"repo": repo, "number": number, "title": f"Change {number}", "body": "Body",
                        "author": p["id"], "created_at": when.isoformat(), "merged_at": (when + timedelta(hours=5)).isoformat(),
                        "files": files, "commits": [{"message": "fix: thing"}, {"message": "feat: other"}],
                        "reviews": [{"reviewer": members[team_of[p["id"]]][0], "body": "ok",
                                     "submitted_at": (when + timedelta(hours=2)).isoformat()}]})
    activity = [{"source": "slack", "kind": "slack_message", "external_id": f"slack-{p['id']}-{k}",
                 "time": (GENERATED_AT - timedelta(days=3 + k)).isoformat(), "person_id": p["id"], "direction": "did",
                 "title": None, "text": f"Update from {p['id']} #{k}", "metadata": {}}
                for p in people for k in range(2)]

    out.mkdir(parents=True, exist_ok=True)
    (out / "roster.json").write_text(json.dumps(roster))
    (out / "github.json").write_text(json.dumps(prs))
    (out / "activity.json").write_text(json.dumps(activity))
    return roster


def fake_muse_handler(roster: dict):
    """Muse stand-in: planted partners score 95, teammates 60-65; t_uni/t_events is the only bridge."""
    partner = {}
    for a, b in roster["planted"].values():
        partner[a], partner[b] = b, a
    team_of = {p["id"]: p["team_id"] for p in roster["people"]}

    def handler(prompt: str, schema) -> dict:
        name = schema.__name__
        if name == "ProfileOut":
            return {"summary": "Does concrete work.", "focus_areas": ["topic a", "topic b"]}
        if name == "RankForPersonOut":
            target = re.search(r"overlaps most with \[(p_\d+)\]", prompt).group(1)
            mates = [p for p in team_of if team_of[p] == team_of[target] and p != target][:3]
            matches = [{"person_id": partner[target], "score": 95, "reason": "They did the same work."}] if target in partner else []
            matches += [{"person_id": m, "score": 65 - i, "reason": "Same team work."} for i, m in enumerate(mates)]
            matches.append({"person_id": "p_999", "score": 99, "reason": "hallucinated id"})
            return {"matches": matches}
        if name == "TeamOverlapsOut":
            return {"pairs": [{"team_a": "t_uni", "team_b": "t_events", "score": 88, "shared_topics": ["hackathon booths"],
                               "summary": "Both run campus events."},
                              {"team_a": "t_payments", "team_b": "t_messaging", "score": 60, "shared_topics": ["kafka"],
                               "summary": "Both use Kafka."}]}
        if name == "LeadBriefOut":
            return {"brief": "**Brief**"}
        raise AssertionError(f"unexpected prompt schema {name}")

    return handler
