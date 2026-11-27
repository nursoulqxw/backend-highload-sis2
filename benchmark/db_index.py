"""SIS #3: what an index BUYS on reads (the cost on writes is experiment E1 in db_experiments.py).

    python -m benchmark.db_index      # needs the seeded DB, ~3 min

Query: "who liked post X" (e.g. a likers list under a post):
    SELECT user_id FROM likes WHERE post_id = $1 LIMIT 20
T7 does not have this endpoint today; this is the price / benefit table we would use to decide.
Without an index on post_id, Postgres must scan all 20M likes. With likes(post_id) it is a lookup.

Output: benchmark/db/index.json and benchmark/db/explain_index.txt
"""

import asyncio
import json
import random
import statistics
import time

import asyncpg

from benchmark.db_bench import DSN, OUT_DIR, run

QUERY = "SELECT user_id FROM likes WHERE post_id = $1 LIMIT 20"


def stats(lat: list[float]) -> dict:
    lat = sorted(lat)
    q = statistics.quantiles(lat, n=100, method="inclusive")
    return {"calls": len(lat), "p50": q[49], "p95": q[94], "p99": q[98], "max": lat[-1]}


async def explain(conn, post_id: int) -> str:
    rows = await conn.fetch(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) {QUERY.replace('$1', str(post_id))}")
    return "\n".join(r[0] for r in rows)


async def main() -> None:
    rng = random.Random(7)
    pool = await asyncpg.create_pool(DSN, min_size=16, max_size=16, command_timeout=600)
    result: dict = {}
    plans = []
    async with pool.acquire() as conn:
        await conn.execute("DROP INDEX IF EXISTS exp_likes_post")
        # Without the index: every call scans the whole table, so only a few calls, one at a time.
        lat = []
        for _ in range(10):
            started = time.perf_counter()
            await conn.fetch(QUERY, rng.randint(1, 100_000))
            lat.append((time.perf_counter() - started) * 1000)
        result["without_index"] = stats(lat)
        plans.append("=== WITHOUT an index on likes(post_id)\n" + await explain(conn, 4242))

        started = time.perf_counter()
        await conn.execute("CREATE INDEX exp_likes_post ON likes (post_id)")
        result["index_build_ms"] = (time.perf_counter() - started) * 1000
        await conn.execute("ANALYZE likes")
        result["index_size"] = await conn.fetchval(
            "SELECT pg_size_pretty(sum(pg_relation_size(relid))) FROM pg_partition_tree('exp_likes_post')"
        )
        plans.append("=== WITH likes(post_id)\n" + await explain(conn, 4242))

    # With the index: thousands of calls, 16 at a time, like the other read benchmarks.
    with_index = await run(pool, lambda c, i: c.fetch(QUERY, rng.randint(1, 100_000)), 5000)
    result["with_index"] = with_index

    async with pool.acquire() as conn:
        await conn.execute("DROP INDEX exp_likes_post")  # T7 does not need it: keep the schema as designed
    await pool.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "index.json").write_text(json.dumps(result, indent=2))
    (OUT_DIR / "explain_index.txt").write_text("\n\n".join(plans) + "\n")
    print(json.dumps(result, indent=2))
    print("\n\n".join(plans))


if __name__ == "__main__":
    asyncio.run(main())
