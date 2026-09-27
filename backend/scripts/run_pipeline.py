"""Batch pipeline (spec §10.4). Idempotent; every Muse call is disk-cached.

    python -m scripts.run_pipeline [--fixtures seed/fixtures]
"""
import argparse
import json
from pathlib import Path

from app.config import settings
from app.db import fetch_all, fetch_one, pool, run_async
from app.expertise import decay, feedback, skill_graph, vectors
from app.expertise.extract import extract_all
from app.pipeline.profiles import build_person_profiles, build_team_profiles
from app.pipeline.similarity import build_similarities
from app.pipeline.team_overlap import build_team_overlaps
from app.pipeline.tasks import backfill_required_skills

BACKEND = Path(__file__).resolve().parent.parent


async def run() -> None:
    await extract_all()                     # 1. expertise: P12 -> P13 -> expertise_events
    await backfill_required_skills()        # 2. open seed tasks get required skills
    await decay.recompute_all()             #    (seed feedback needs skill state for shared skills)
    await feedback.apply_seed()             # 3. seed ratings -> match_feedback + evidence
    await skill_graph.rebuild()             # 4. decay + skill graph (+ focus areas)
    await decay.recompute_all()
    await build_person_profiles()           # 5. profiles
    await build_team_profiles()
    if settings.MATCHER_MODE == "hybrid":
        raise NotImplementedError("hybrid embeddings are Phase 4")
    await build_similarities()
    await build_team_overlaps()


async def report(planted: dict[str, list[str]], planted_temporal: dict[str, str] | None = None) -> list[str]:
    """Print the §9.4 report; return a list of problems (empty = all acceptance checks pass)."""
    problems = []
    print("\nPlanted pairs (need rank 1-2 and score >= AUTO_NOTIFY_THRESHOLD; amber_case rank only):")
    for name, (x, y) in planted.items():
        a, b = sorted((x, y))
        row = await fetch_one("""SELECT score, LEAST(rank_a, rank_b) AS rank FROM similarities
                                 WHERE person_a = %s AND person_b = %s""", (a, b))
        ok = row and row["rank"] is not None and row["rank"] <= 2
        # amber_case is the "faint match" (§16 step 6): it must be below the auto-notify bar,
        # otherwise Sam's task would have been auto-connected and never gone amber.
        if name != "amber_case":
            ok = ok and row["score"] >= settings.AUTO_NOTIFY_THRESHOLD
        print(f"  {'OK  ' if ok else 'FAIL'} {name:<16} {a}-{b}  "
              + (f"rank {row['rank']}  score {row['score']:.2f}" if row else "no edge"))
        if not ok:
            problems.append(f"planted pair {name} not surfaced")

    await report_skills(planted)
    if planted_temporal:
        problems += await report_temporal(planted, planted_temporal)

    missed = await fetch_one("SELECT count(*) AS n FROM similarities WHERE score >= %s", (settings.MISSED_MIN_SCORE,))
    print(f"\nPairs >= MISSED_MIN_SCORE ({settings.MISSED_MIN_SCORE}): {missed['n']}")

    bridges = await fetch_all("SELECT team_a, team_b, score FROM team_overlaps WHERE score >= %s ORDER BY score DESC",
                              (settings.BRIDGE_MIN_SCORE,))
    print(f"Bridges >= BRIDGE_MIN_SCORE ({settings.BRIDGE_MIN_SCORE}):")
    for b in bridges:
        print(f"  {b['team_a']} <-> {b['team_b']}  {b['score']:.2f}")
    if not any({b["team_a"], b["team_b"]} == {"t_uni", "t_events"} for b in bridges):
        problems.append("t_uni/t_events bridge missing")

    thin = await fetch_all("""
        SELECT p.id, count(s.*) AS n FROM people p
        LEFT JOIN similarities s ON p.id IN (s.person_a, s.person_b)
        GROUP BY p.id HAVING count(s.*) < 3 ORDER BY p.id""")
    if thin:
        problems.append(f"{len(thin)} people with < 3 matches: {', '.join(t['id'] for t in thin)}")
    print("\n" + ("All acceptance checks pass." if not problems else "PROBLEMS:\n  " + "\n  ".join(problems)))
    return problems


async def report_temporal(planted: dict[str, list[str]], pt: dict[str, str]) -> list[str]:
    """planted_temporal checks: recency beats a single old example; a newly learned skill shows up."""
    problems = []
    print("\nPlanted temporal:")
    payments, messaging = planted["kafka_lag"]
    stale = pt["stale_expert"]

    async def score(x, y):
        a, b = sorted((x, y))
        row = await fetch_one("SELECT score, temporal_score FROM similarities WHERE person_a = %s AND person_b = %s", (a, b))
        return (row["score"], row["temporal_score"]) if row else (0.0, 0.0)

    (fresh_s, fresh_t), (stale_s, stale_t) = await score(messaging, payments), await score(messaging, stale)
    ok = fresh_s > stale_s
    print(f"  {'OK  ' if ok else 'FAIL'} stale_expert     for {messaging}: {payments} {fresh_s:.2f} (temporal {fresh_t:.2f})"
          f" vs stale {stale} {stale_s:.2f} (temporal {stale_t:.2f})")
    if not ok:
        problems.append("stale_expert outranks the recent kafka engineer")

    emerging = pt["emerging_skill"]
    new = await fetch_all("""SELECT s.name, st.level FROM person_skill_state st JOIN skills s ON s.id = st.skill_id
                             WHERE st.person_id = %s AND st.trend = 'new' ORDER BY st.level DESC""", (emerging,))
    task = await fetch_one("SELECT required_skills FROM tasks WHERE person_id = %s AND source_event_id IS NULL "
                           "AND status IN ('open', 'notified') ORDER BY created_at LIMIT 1", (planted["amber_case"][0],))
    space = await vectors.load()
    top = [p for p, _ in space.rank_for_skills(task["required_skills"] if task else [], {planted["amber_case"][0]})[:3]]
    ok = bool(new) and emerging in top
    new_skills = ", ".join("%s (%.2f)" % (r["name"], r["level"]) for r in new) or "(none)"
    print(f"  {'OK  ' if ok else 'FAIL'} emerging_skill   {emerging} new skills: {new_skills}; "
          f"top-3 task candidates for the amber task: {', '.join(top) or '(none)'}")
    if not ok:
        problems.append("emerging_skill not surfaced (trend 'new' + top 3 for the amber task)")
    return problems


async def report_skills(planted: dict[str, list[str]]) -> None:
    """Skill vocabulary (target 60-120), the largest skills, and what each planted pair shares."""
    n = await fetch_one("""SELECT (SELECT count(*) FROM skills) AS skills, (SELECT count(*) FROM skill_labels) AS labels,
                                  (SELECT count(*) FROM expertise_events) AS evidence""")
    print(f"\nSkills: {n['skills']} (target 60-120) from {n['labels']} raw labels; {n['evidence']} evidence rows")
    for r in await fetch_all("""SELECT s.name, count(*) AS n, count(DISTINCT x.person_id) AS people
                                FROM expertise_events x JOIN skills s ON s.id = x.skill_id
                                GROUP BY s.name ORDER BY n DESC, s.name LIMIT 10"""):
        print(f"  {r['n']:>4} evidence  {r['people']:>2} people  {r['name']}")
    print("Skills shared by planted pairs:")
    for name, (a, b) in planted.items():
        shared = await fetch_all("""SELECT s.name FROM skills s WHERE
                                      EXISTS (SELECT 1 FROM expertise_events WHERE person_id = %s AND skill_id = s.id)
                                      AND EXISTS (SELECT 1 FROM expertise_events WHERE person_id = %s AND skill_id = s.id)
                                    ORDER BY s.name""", (a, b))
        print(f"  {name:<16} {', '.join(r['name'] for r in shared) or '(none)'}")


async def main(fixtures: Path) -> None:
    roster = json.loads((fixtures / "roster.json").read_text(encoding="utf-8"))
    await pool.open()
    try:
        await run()
        await report(roster["planted"], roster.get("planted_temporal"))
    finally:
        await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", type=Path, default=BACKEND / "seed" / "fixtures")
    run_async(main(ap.parse_args().fixtures))
