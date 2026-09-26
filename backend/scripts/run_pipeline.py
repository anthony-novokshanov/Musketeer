"""Batch pipeline (spec §9.4). Idempotent; every Muse call is disk-cached.

    python -m scripts.run_pipeline [--fixtures seed/fixtures]
"""
import argparse
import json
from pathlib import Path

from app.config import settings
from app.db import fetch_all, fetch_one, pool, run_async
from app.pipeline.profiles import build_person_profiles, build_team_profiles
from app.pipeline.similarity import build_similarities
from app.pipeline.team_overlap import build_team_overlaps

BACKEND = Path(__file__).resolve().parent.parent


async def run() -> None:
    await build_person_profiles()
    await build_team_profiles()
    if settings.MATCHER_MODE == "hybrid":
        raise NotImplementedError("hybrid embeddings are Phase 4")
    await build_similarities()
    await build_team_overlaps()


async def report(planted: dict[str, list[str]]) -> list[str]:
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


async def main(fixtures: Path) -> None:
    planted = json.loads((fixtures / "roster.json").read_text(encoding="utf-8"))["planted"]
    await pool.open()
    try:
        await run()
        await report(planted)
    finally:
        await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", type=Path, default=BACKEND / "seed" / "fixtures")
    run_async(main(ap.parse_args().fixtures))
