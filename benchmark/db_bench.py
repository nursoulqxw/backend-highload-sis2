"""SIS #3: benchmark of the real SQL queries of T7, straight to Postgres (no API, no Redis).

    python -m benchmark.db_bench --label before   # on the old schema
    python -m benchmark.db_bench --label after    # after the tuning
    python -m benchmark.db_bench --report         # compare before vs after -> benchmark/db_results.md

Every query is the one the service really sends (see app/core). Each runs with 16
connections at the same time, like 16 busy server processes.

  Q1 like          INSERT INTO likes ... ON CONFLICT DO NOTHING           (a new pair each time)
  Q3 cold read x1  SELECT ... FROM post_counters WHERE post_id = ANY($1)  (1 random post of 50M)
  Q3 cold read x100  the same with 100 random posts (GetCounters after a Redis restart)
  Q4 liked_by_me   SELECT post_id FROM likes WHERE user_id = $1 AND post_id = ANY($2)
  Q5 sync batch    upsert 1000 counters (what the sync worker does every 5 s)
                   before: executemany (1000 statements), after: one statement with unnest
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "benchmark" / "db"
DSN = os.environ.get("BENCH_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/counters")
POSTS = 50_000_000
USERS = 10_000_000  # users 1..10M have 2 likes each (db/seed.sql)
CONCURRENCY = 16

UPSERT_ONE = """
INSERT INTO post_counters (post_id, likes, views, updated_at) VALUES ($1, $2, $3, now())
ON CONFLICT (post_id) DO UPDATE SET likes = EXCLUDED.likes, views = EXCLUDED.views, updated_at = now()
"""
UPSERT_UNNEST = """
INSERT INTO post_counters (post_id, likes, views, updated_at)
SELECT p, l, v, now() FROM unnest($1::bigint[], $2::bigint[], $3::bigint[]) AS t(p, l, v)
ON CONFLICT (post_id) DO UPDATE SET likes = EXCLUDED.likes, views = EXCLUDED.views, updated_at = now()
"""


def rnd_post(rng: random.Random) -> int:
    # Same skew as the load test: 90% hot (100K posts), 10% any of the 50M.
    return rng.randint(1, 100_000) if rng.random() < 0.9 else rng.randint(1, POSTS)


async def run(pool: asyncpg.Pool, make_call, count: int) -> dict:
    """`count` calls, CONCURRENCY at a time. Returns latency stats in ms and calls per second."""
    numbers = iter(range(count))
    lat: list[float] = []

    async def worker() -> None:
        async with pool.acquire() as conn:
            for i in numbers:
                started = time.perf_counter()
                await make_call(conn, i)
                lat.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
    seconds = time.perf_counter() - started
    lat.sort()
    q = statistics.quantiles(lat, n=100)
    return {
        "calls": count,
        "per_s": count / seconds,
        "p50": q[49],
        "p95": q[94],
        "p99": q[98],
        "max": lat[-1],
    }


async def wal_bytes(conn) -> int:
    return await conn.fetchval("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint")


async def bench(label: str, scale: float) -> dict:
    pool = await asyncpg.create_pool(DSN, min_size=CONCURRENCY, max_size=CONCURRENCY, command_timeout=60)
    rng = random.Random(42)
    n = lambda base: max(200, int(base * scale))  # noqa: E731
    results: dict = {"label": label}

    # Warm-up: the same mix, not measured.
    warm_sql = "SELECT likes FROM post_counters WHERE post_id = $1"
    await run(pool, lambda c, i: c.fetch(warm_sql, rnd_post(rng)), 2000)

    user_base = 100_000_000 + int(time.time()) % 1_000_000 * 100  # new pairs every run
    results["Q1 like"] = await run(
        pool,
        lambda c, i: c.execute(
            "INSERT INTO likes (user_id, post_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            user_base + i,
            rnd_post(rng),
        ),
        n(20_000),
    )
    results["Q3 cold read x1"] = await run(
        pool,
        lambda c, i: c.fetch(
            "SELECT post_id, likes, views FROM post_counters WHERE post_id = ANY($1::bigint[])",
            [rng.randint(1, POSTS)],
        ),
        n(20_000),
    )
    results["Q3 cold read x100"] = await run(
        pool,
        lambda c, i: c.fetch(
            "SELECT post_id, likes, views FROM post_counters WHERE post_id = ANY($1::bigint[])",
            [rng.randint(1, POSTS) for _ in range(100)],
        ),
        n(3_000),
    )
    results["Q4 liked_by_me x20"] = await run(
        pool,
        lambda c, i: c.fetch(
            "SELECT post_id FROM likes WHERE user_id = $1 AND post_id = ANY($2::bigint[])",
            rng.randint(1, USERS),
            [rnd_post(rng) for _ in range(20)],
        ),
        n(20_000),
    )

    # Q5: the sync worker. 1000 dirty hot posts per batch, run one batch at a time like the worker.
    async with pool.acquire() as conn:
        for mode, sql in (("executemany", UPSERT_ONE), ("unnest", UPSERT_UNNEST)):
            stats_before = await conn.fetchrow(
                "SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE relname = 'post_counters'"
            )
            wal_start = await wal_bytes(conn)
            lat = []
            for b in range(n(100)):
                ids = sorted(rng.sample(range(1, 100_001), 1000))  # sorted: fewer lock waits / page jumps
                likes = [rng.randint(0, 500) for _ in ids]
                views = [x * 30 + b for x in likes]
                started = time.perf_counter()
                if mode == "executemany":
                    await conn.executemany(sql, list(zip(ids, likes, views, strict=True)))
                else:
                    await conn.execute(sql, ids, likes, views)
                lat.append((time.perf_counter() - started) * 1000)
            wal_used = await wal_bytes(conn) - wal_start
            await asyncio.sleep(1)  # let the stats collector catch up
            stats_after = await conn.fetchrow(
                "SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE relname = 'post_counters'"
            )
            upd = stats_after["n_tup_upd"] - stats_before["n_tup_upd"]
            hot = stats_after["n_tup_hot_upd"] - stats_before["n_tup_hot_upd"]
            lat.sort()
            q = statistics.quantiles(lat, n=100)
            results[f"Q5 sync 1000 ({mode})"] = {
                "calls": len(lat),
                "per_s": 1000 / statistics.mean(lat) * 1000,  # rows per second
                "p50": q[49],
                "p95": q[94],
                "p99": q[98],
                "max": lat[-1],
                "wal_kb_per_batch": wal_used / len(lat) / 1024,
                "hot_update_pct": 100 * hot / upd if upd else 0,
            }

    # Size of tables + indexes, and buffer cache hit ratio.
    async with pool.acquire() as conn:
        results["sizes"] = {
            r["name"]: r["size"]
            for r in await conn.fetch(
                """
                SELECT c.relname AS name, pg_size_pretty(pg_total_relation_size(c.oid)) AS size
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'i', 'I')
                  AND c.relname NOT LIKE '%\\_p%' ESCAPE '\\'
                ORDER BY pg_total_relation_size(c.oid) DESC
                """
            )
        }
        results["cache_hit_pct"] = await conn.fetchval(
            "SELECT round(100.0 * sum(blks_hit) / nullif(sum(blks_hit) + sum(blks_read), 0), 2)::float"
            " FROM pg_stat_database WHERE datname = current_database()"
        )
        results["settings"] = {
            r["name"]: r["setting"] + (r["unit"] or "")
            for r in await conn.fetch(
                "SELECT name, setting, unit FROM pg_settings WHERE name = ANY($1)",
                ["shared_buffers", "effective_cache_size", "random_page_cost", "work_mem", "max_wal_size"],
            )
        }
    await pool.close()
    return results


def report() -> None:
    before = json.loads((OUT_DIR / "before.json").read_text())
    after = json.loads((OUT_DIR / "after.json").read_text())
    queries = [k for k in before if k.startswith("Q") and k in after]
    # Q5 "before" = executemany on the old schema, "after" = unnest on the new one.
    lines = [
        "# SIS #3: database before / after (direct SQL, 16 connections)",
        "",
        "| Query | before p50 | before p95 | before p99 | after p50 | after p95 | after p99 | p99 gain"
        " | before /s | after /s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for q in queries:
        b, a = before[q], after[q]
        lines.append(
            f"| {q} | {b['p50']:.2f} | {b['p95']:.2f} | {b['p99']:.2f} | {a['p50']:.2f} | {a['p95']:.2f}"
            f" | {a['p99']:.2f} | {b['p99'] / a['p99']:.1f}x | {b['per_s']:,.0f} | {a['per_s']:,.0f} |"
        )
    lines += ["", "Times in ms. Q5 rows/s = rows written per second.", ""]
    for q in queries:
        if "wal_kb_per_batch" in before[q]:
            lines.append(
                f"- {q}: WAL {before[q]['wal_kb_per_batch']:.0f} -> {after[q]['wal_kb_per_batch']:.0f}"
                " KB per batch,"
                f" HOT updates {before[q]['hot_update_pct']:.0f}% -> {after[q]['hot_update_pct']:.0f}%"
            )
    lines += [
        f"- Buffer cache hit ratio: {before['cache_hit_pct']}% -> {after['cache_hit_pct']}%",
        f"- Settings before: {before['settings']}",
        f"- Settings after: {after['settings']}",
        f"- Sizes before: {before['sizes']}",
        f"- Sizes after: {after['sizes']}",
        "",
    ]
    (ROOT / "benchmark" / "db_results.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", choices=["before", "after"])
    parser.add_argument("--scale", type=float, default=1.0, help="0.1 = 10x fewer calls (quick check)")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.label:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        result = asyncio.run(bench(args.label, args.scale))
        (OUT_DIR / f"{args.label}.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    if args.report:
        report()
