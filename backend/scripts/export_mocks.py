"""Dump real API responses into frontend/src/mocks (spec §3, §12.1) for VITE_USE_MOCKS=true.

    python -m scripts.export_mocks

graph.json and synopsis.json are single responses; people/pairs/bridges.json map an id
("p_001", "p_001|p_002", "t_a|t_b") to its response; search.json maps a query to its response.
"""
import json
from pathlib import Path

import httpx

from app.db import pool, run_async
from app.main import app

OUT = Path(__file__).resolve().parents[2] / "frontend" / "src" / "mocks"
SEARCH_QUERIES = ["who has built rate limiting?", "who has run a hackathon booth?"]


async def main() -> None:
    await pool.open()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock",
                                     timeout=120) as c:
            async def get(path: str, **params) -> dict:
                r = await c.get(f"/api{path}", params=params)
                r.raise_for_status()
                return r.json()

            graph = await get("/graph")
            mocks = {
                "graph": graph,
                "synopsis": await get("/synopsis", days=30),
                "people": {p["id"]: await get(f"/people/{p['id']}") for p in graph["people"]},
                "pairs": {f"{e['a']}|{e['b']}": await get(f"/pairs/{e['a']}/{e['b']}") for e in graph["edges"]},
                "bridges": {f"{b['team_a']}|{b['team_b']}": await get(f"/bridges/{b['team_a']}/{b['team_b']}")
                            for b in graph["bridges"]},
                "search": {q: await get("/search", q=q) for q in SEARCH_QUERIES},
            }
    finally:
        await pool.close()

    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in mocks.items():
        (OUT / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"  {name}.json" + (f"  ({len(data)} entries)" if name in ("people", "pairs", "bridges", "search") else ""))
    print(f"Wrote mocks to {OUT}")


if __name__ == "__main__":
    run_async(main())
