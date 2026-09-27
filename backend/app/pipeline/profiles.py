"""Person/team profiles (P2, P3), GitHub aggregates (§9.2), expertise cards and the roster block (§8.3)."""
import asyncio
import json
import logging

from app.ai.muse import muse_json
from app.db import execute, fetch_all
from app.schemas import ProfileOut

log = logging.getLogger("musketeer.profiles")

_GITHUB_WINDOW = """source = 'github' AND kind IN ('pr', 'review') AND direction = 'did'
                    AND time > now() - INTERVAL '60 days'"""


async def github_aggregates() -> dict[str, dict]:
    """Per engineer over 60 days: top directories (by count), all directories, languages, commit mix."""
    dirs = await fetch_all(f"""
        SELECT person_id, d AS dir, count(*) AS n
        FROM activity_events, jsonb_array_elements_text(metadata->'directories') d
        WHERE {_GITHUB_WINDOW} GROUP BY 1, 2 ORDER BY person_id, n DESC, dir""")
    langs = await fetch_all(f"""
        SELECT person_id, l AS lang, count(*) AS n
        FROM activity_events, jsonb_array_elements_text(metadata->'languages') l
        WHERE {_GITHUB_WINDOW} GROUP BY 1, 2 ORDER BY person_id, n DESC, lang""")
    mix = await fetch_all(f"""
        SELECT person_id, key AS type, sum(value::int) AS n
        FROM activity_events, jsonb_each_text(metadata->'commit_types')
        WHERE {_GITHUB_WINDOW} AND kind = 'pr' GROUP BY 1, 2 ORDER BY person_id, n DESC, type""")

    out: dict[str, dict] = {}
    blank = lambda: {"top_directories": [], "all_directories": set(), "languages": [], "commit_mix": {}}
    for r in dirs:
        agg = out.setdefault(r["person_id"], blank())
        agg["all_directories"].add(r["dir"])
        if len(agg["top_directories"]) < 5:
            agg["top_directories"].append(r["dir"])
    for r in langs:
        out.setdefault(r["person_id"], blank())["languages"].append(r["lang"])
    for r in mix:
        out.setdefault(r["person_id"], blank())["commit_mix"][r["type"]] = int(r["n"])
    return out


async def engineer_dirs() -> dict[str, set[str]]:
    """GitHub directory set per engineer (empty for everyone else), for dir_overlap and candidates."""
    gh = await github_aggregates()
    people = await fetch_all("SELECT id, is_engineer FROM people")
    return {p["id"]: gh[p["id"]]["all_directories"] if p["is_engineer"] and p["id"] in gh else set() for p in people}


CARD_SKILLS = 6
CARD_SNIPPETS = 2


def _ago(computed_at, last_seen) -> str:
    """Days relative to the state's computed_at, so cards stay byte-identical between refreshes."""
    return f"{max(0, (computed_at - last_seen).days)}d ago"


async def skill_rows(person_ids: list[str] | None = None, limit: int = CARD_SKILLS) -> dict[str, list[dict]]:
    """Top skills by level per person, from person_skill_state."""
    rows = await fetch_all("""
        SELECT * FROM (
            SELECT st.*, s.name, row_number() OVER (PARTITION BY st.person_id ORDER BY st.level DESC, s.name) AS rn
            FROM person_skill_state st JOIN skills s ON s.id = st.skill_id
            WHERE %(pids)s::text[] IS NULL OR st.person_id = ANY(%(pids)s)) r
        WHERE rn <= %(limit)s ORDER BY person_id, rn""", {"pids": person_ids, "limit": limit})
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["person_id"], []).append(r)
    return out


def format_skills(skills: list[dict], snippets: int = CARD_SNIPPETS) -> str:
    parts = []
    for i, k in enumerate(skills):
        detail = f"{k['level']:.2f}, {k['trend']}, {_ago(k['computed_at'], k['last_seen'])}"
        if i < snippets:
            detail += f': "{k["best_snippet"]}"'
        parts.append(f"{k['name']} ({detail})")
    return "; ".join(parts) or "none yet"


async def expertise_cards(person_ids: list[str] | None = None) -> dict[str, str]:
    """The temporal graph rendered as text for Muse (spec §8.3), keyed by person id."""
    people = await fetch_all("""
        SELECT p.id, p.name, p.title, p.is_engineer, p.summary, t.name AS team, o.name AS org
        FROM people p JOIN teams t ON t.id = p.team_id JOIN orgs o ON o.id = t.org_id
        WHERE %(pids)s::text[] IS NULL OR p.id = ANY(%(pids)s) ORDER BY p.id""", {"pids": person_ids})
    skills = await skill_rows(person_ids)
    gh = await github_aggregates()
    cards = {}
    for p in people:
        card = (f"[{p['id']}] {p['name']} | {p['title']} | {p['team']} ({p['org']})\n"
                f"Summary: {p['summary'] or ''}\n"
                f"Skills (level, trend, last seen): {format_skills(skills.get(p['id'], []))}")
        if p["is_engineer"] and p["id"] in gh:
            card += (f"\nCode: {', '.join(gh[p['id']]['top_directories'])}"
                     f" | Langs: {', '.join(gh[p['id']]['languages'])}")
        cards[p["id"]] = card
    return cards


async def roster_block() -> str:
    """Every expertise card, ordered by id so the prefix is byte-identical between refreshes."""
    return "\n\n".join((await expertise_cards()).values())


async def build_person_profiles() -> None:
    people = await fetch_all("""
        SELECT p.id, p.name, p.title, t.name AS team FROM people p JOIN teams t ON t.id = p.team_id ORDER BY p.id""")
    gh = await github_aggregates()
    skills = await skill_rows(limit=10)

    async def one(p: dict) -> None:
        events = await fetch_all("""
            SELECT time, kind, text FROM activity_events
            WHERE person_id = %s AND direction = 'did' ORDER BY time DESC LIMIT 20""", (p["id"],))
        agg = gh.get(p["id"])
        github = "none" if not agg else json.dumps({k: v for k, v in agg.items() if k != "all_directories"})
        out = await muse_json("person_profile", {
            "name": p["name"], "title": p["title"], "team": p["team"], "github": github,
            "skills": format_skills(skills.get(p["id"], []), snippets=10),
            "events": "\n".join(f"- {e['time']:%Y-%m-%d} [{e['kind']}] {e['text']}" for e in events) or "none",
        }, ProfileOut)
        await execute("UPDATE people SET summary = %s WHERE id = %s", (out.summary, p["id"]))

    await asyncio.gather(*(one(p) for p in people))
    log.info("profiled %d people", len(people))


async def build_team_profiles() -> None:
    teams = await fetch_all("SELECT id, name, focus_areas FROM teams ORDER BY id")

    async def one(t: dict) -> None:
        members = await fetch_all("SELECT name, title, summary FROM people WHERE team_id = %s ORDER BY id", (t["id"],))
        out = await muse_json("team_profile", {
            "team_name": t["name"], "top_skills": ", ".join(t["focus_areas"]) or "none yet",
            "member_summaries": "\n".join(f"- {m['name']} ({m['title']}): {m['summary'] or ''}" for m in members),
        }, ProfileOut)
        await execute("UPDATE teams SET summary = %s WHERE id = %s", (out.summary, t["id"]))

    await asyncio.gather(*(one(t) for t in teams))
    log.info("profiled %d teams", len(teams))
