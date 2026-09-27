"""Scale benchmark for hybrid retrieval: N synthetic 768-dim profile vectors in pgvector with a
StreamingDiskANN index (pgvectorscale), against an exact scan: server-side latency and recall@10.
Runs in a throwaway `musketeer_bench` schema that is dropped at the end; real tables are never touched.

    python -m scripts.scale_bench [--people 20000] [--queries 50]
"""
import argparse
import statistics
import time

import numpy as np
import psycopg

from app.config import settings
from app.db import run_async

SCHEMA = "musketeer_bench"
SQL = "SELECT id FROM people ORDER BY embedding <=> %s::vector LIMIT 10"


def vectors(n: int, seed: int) -> np.ndarray:
    """Clustered unit vectors (people cluster by kind of work, like real profile embeddings).
    Centers come from a fixed seed so queries land in the same clusters as the data."""
    centers = np.random.default_rng(7).normal(size=(200, 768))
    rng = np.random.default_rng(seed)
    v = centers[rng.integers(0, len(centers), n)] + 0.6 * rng.normal(size=(n, 768))
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def lit(v) -> str:
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


async def main(n: int, queries: int) -> None:
    data, qs = vectors(n, 1), vectors(queries, 2)
    async with await psycopg.AsyncConnection.connect(settings.DATABASE_URL, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {SCHEMA}")
        await conn.execute(f"SET search_path = {SCHEMA}, public")
        try:
            await conn.execute("CREATE TABLE people (id int PRIMARY KEY, embedding vector(768))")
            t = time.perf_counter()
            async with conn.cursor() as cur, cur.copy("COPY people (id, embedding) FROM STDIN") as cp:
                for i, v in enumerate(data):
                    await cp.write_row((i, lit(v)))
            print(f"loaded {n:,} vectors in {time.perf_counter() - t:.1f}s")
            t = time.perf_counter()
            await conn.execute("CREATE INDEX ON people USING diskann (embedding vector_cosine_ops)")
            print(f"built StreamingDiskANN index in {time.perf_counter() - t:.1f}s")
            await conn.execute("ANALYZE people")
            await conn.execute("SET diskann.query_search_list_size = 200")
            await conn.execute("SET diskann.query_rescore = 200")

            async def top(q: str, exact: bool) -> tuple[list[int], float]:
                """(ids, server-side execution ms): network latency to Tiger Cloud isn't counted."""
                await conn.execute(f"SET enable_indexscan = {'off' if exact else 'on'}")
                rows = await (await conn.execute(SQL, (q,))).fetchall()
                [[plan]] = await (await conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + SQL, (q,))).fetchall()
                used = "Index Scan" in plan[0]["Plan"]["Plans"][0]["Node Type"]
                assert used != exact, "unexpected plan"
                return [r[0] for r in rows], plan[0]["Execution Time"]

            ann, exact, recall = [], [], []
            for q in qs:
                got, ms = await top(lit(q), exact=False)
                truth, ems = await top(lit(q), exact=True)
                ann.append(ms), exact.append(ems), recall.append(len(set(got) & set(truth)) / 10)
            p = lambda xs, k: statistics.quantiles(xs, n=100)[k - 1]
            print(f"top-10 over {n:,} people, {queries} queries, server-side execution time:")
            print(f"  StreamingDiskANN  p50 {p(ann, 50):.1f} ms   p95 {p(ann, 95):.1f} ms")
            print(f"  exact scan        p50 {p(exact, 50):.1f} ms   p95 {p(exact, 95):.1f} ms")
            print(f"  recall@10         {statistics.mean(recall):.3f}")
        finally:
            await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--people", type=int, default=20000)
    ap.add_argument("--queries", type=int, default=50)
    a = ap.parse_args()
    run_async(main(a.people, a.queries))
