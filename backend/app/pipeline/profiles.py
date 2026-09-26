"""Person/team profiles (P2, P3), GitHub aggregates (§8.2) and the roster block (§7.3)."""
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


async def roster_block() -> str:
    """One line per person, ordered by id so the prefix is byte-identical across calls."""
    people = await fetch_all("""
        SELECT p.id, p.name, p.title, p.is_engineer, p.summary, p.focus_areas, t.name AS team, o.name AS org
        FROM people p JOIN teams t ON t.id = p.team_id JOIN orgs o ON o.id = t.org_id
        ORDER BY p.id""")
    gh = await github_aggregates()
    lines = []
    for p in people:
        line = (f"[{p['id']}] {p['name']} | {p['title']} | {p['team']} ({p['org']}) | "
                f"Summary: {p['summary'] or ''} | Focus: {', '.join(p['focus_areas'])}")
        if p["is_engineer"] and p["id"] in gh:
            line += (f" | Code: {', '.join(gh[p['id']]['top_directories'])}"
                     f" | Langs: {', '.join(gh[p['id']]['languages'])}")
        lines.append(line)
    return "\n".join(lines)


async def build_person_profiles() -> None:
    people = await fetch_all("""
        SELECT p.id, p.name, p.title, t.name AS team FROM people p JOIN teams t ON t.id = p.team_id ORDER BY p.id""")
    gh = await github_aggregates()

    async def one(p: dict) -> None:
        events = await fetch_all("""
            SELECT time, kind, text FROM activity_events
            WHERE person_id = %s AND direction = 'did' ORDER BY time DESC LIMIT 40""", (p["id"],))
        agg = gh.get(p["id"])
        github = "none" if not agg else json.dumps({k: v for k, v in agg.items() if k != "all_directories"})
        out = await muse_json("person_profile", {
            "name": p["name"], "title": p["title"], "team": p["team"], "github": github,
            "events": "\n".join(f"- {e['time']:%Y-%m-%d} [{e['kind']}] {e['text']}" for e in events) or "none",
        }, ProfileOut)
        await execute("UPDATE people SET summary = %s, focus_areas = %s WHERE id = %s",
                      (out.summary, out.focus_areas[:6], p["id"]))

    await asyncio.gather(*(one(p) for p in people))
    log.info("profiled %d people", len(people))


async def build_team_profiles() -> None:
    teams = await fetch_all("SELECT id, name FROM teams ORDER BY id")

    async def one(t: dict) -> None:
        members = await fetch_all("SELECT name, title, summary FROM people WHERE team_id = %s ORDER BY id", (t["id"],))
        out = await muse_json("team_profile", {
            "team_name": t["name"],
            "member_summaries": "\n".join(f"- {m['name']} ({m['title']}): {m['summary'] or ''}" for m in members),
        }, ProfileOut)
        await execute("UPDATE teams SET summary = %s, focus_areas = %s WHERE id = %s",
                      (out.summary, out.focus_areas[:6], t["id"]))

    await asyncio.gather(*(one(t) for t in teams))
    log.info("profiled %d teams", len(teams))
