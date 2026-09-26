"""Download a stock headshot for every person into static/avatars/<person_id>.jpg (served at /avatars/).

    python -m scripts.fetch_avatars [--force]

Portraits come from randomuser.me (free placeholder photos for mockups). The seed people are fictional;
photos are picked by first name so the set looks plausible, and each person gets a different photo.
Stored locally so the demo doesn't depend on an outside site.
"""
import argparse
from pathlib import Path

import psycopg
import requests

from app.config import settings

OUT = Path(__file__).resolve().parent.parent / "static" / "avatars"
WOMEN = {"Maya", "Sofia", "Aisha", "Elena", "Amara", "Rachel", "Grace", "Hannah", "Emily", "Fatima", "Zoe", "Ingrid",
         "Diana", "Mei", "Sarah", "Nia", "Laura", "Jessica", "Nina", "Olivia", "Isabella"}


def main(force: bool) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(settings.DATABASE_URL) as db:
        people = db.execute("SELECT id, name FROM people ORDER BY id").fetchall()
    counters = {"women": 0, "men": 0}
    for pid, name in people:
        kind = "women" if name.split()[0] in WOMEN else "men"
        idx = 10 + counters[kind] * 3            # spread across the set, never the same photo twice
        counters[kind] += 1
        dest = OUT / f"{pid}.jpg"
        if dest.exists() and not force:
            continue
        r = requests.get(f"https://randomuser.me/api/portraits/{kind}/{idx}.jpg", timeout=20)
        r.raise_for_status()
        dest.write_bytes(r.content)
    print(f"{len(list(OUT.glob('*.jpg')))} avatars in {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-download even if the file exists")
    main(ap.parse_args().force)
