"""SIS #3 experiments: each one answers one design question with numbers.

    python -m benchmark.db_experiments      # needs the seeded DB (db/seed.sql), ~10 min

E1  Cost of an index.        Would `likes(post_id)` help? What does it cost on every like?
E2  Which partitioning?      none vs HASH(user_id) vs RANGE(created_at) per week / per day,
                             for OUR query, and DELETE vs DROP PARTITION to remove old data.
E3  Flag vs watermark.       How does the sync worker find changed posts? A watermark
                             (WHERE updated_at > X) needs an index on updated_at -> no HOT updates.
                             We use a flag instead: the Redis set dirty_posts.

Output: benchmark/db/experiments.json (+ printed).
"""

import asyncio
import json
import random
import statistics
import time

import asyncpg

from benchmark.db_bench import DSN, OUT_DIR, UPSERT_UNNEST, run

SAMPLE = 5_000_000  # rows for the partitioning tables (a copy of 1/4 of likes)

# Size of a table or index, together with all its partitions (works for plain ones too).
SIZE_SQL = "SELECT pg_size_pretty(sum(pg_total_relation_size(relid))) FROM pg_partition_tree($1)"


def ms_stats(lat: list[float]) -> dict:
    lat = sorted(lat)
    q = statistics.quantiles(lat, n=100)
    return {"p50": q[49], "p95": q[94], "p99": q[98], "max": lat[-1]}


async def wal(conn) -> int:
    return await conn.fetchval("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint")


async def timed(conn, sql: str, *args) -> float:
    started = time.perf_counter()
    await conn.execute(sql, *args)
    return (time.perf_counter() - started) * 1000


# ---------- E1 ----------


async def e1_index_cost(pool) -> dict:
    """Insert likes with and without an extra index on likes(post_id)."""
    rng = random.Random(1)
    base = 700_000_000 + int(time.time()) % 100_000 * 1000

    async def inserts(offset: int) -> dict:
        return await run(
            pool,
            lambda c, i: c.execute(
                "INSERT INTO likes (user_id, post_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                base + offset + i,
                rng.randint(1, 50_000_000),
            ),
            20_000,
        )

    out = {"without": await inserts(0)}
    async with pool.acquire() as conn:
        build_ms = await timed(conn, "CREATE INDEX exp_likes_post ON likes (post_id)")
        size = await conn.fetchval(SIZE_SQL, "exp_likes_post")
    out["with_post_id_index"] = await inserts(1_000_000)
    async with pool.acquire() as conn:
        await conn.execute("DROP INDEX exp_likes_post")
    out["index_build_ms"] = build_ms
    out["index_size"] = size
    return out


# ---------- E2 ----------

LAYOUTS = {
    "none": "",
    "hash16_user": "PARTITION BY HASH (user_id)",
    "range_week": "PARTITION BY RANGE (created_at)",
    "range_day": "PARTITION BY RANGE (created_at)",
}


async def make_layout(conn, name: str) -> None:
    table = f"exp_{name}"
    await conn.execute(f"DROP TABLE IF EXISTS {table}")
    # The primary key must contain the partition key -> for RANGE it is (user_id, post_id, created_at).
    pk = "(user_id, post_id, created_at)" if name.startswith("range") else "(user_id, post_id)"
    await conn.execute(
        f"CREATE TABLE {table} (user_id bigint NOT NULL, post_id bigint NOT NULL,"
        f" created_at timestamptz NOT NULL, PRIMARY KEY {pk}) {LAYOUTS[name]}"
    )
    if name == "hash16_user":
        for i in range(16):
            await conn.execute(
                f"CREATE TABLE {table}_{i} PARTITION OF {table} FOR VALUES WITH (MODULUS 16, REMAINDER {i})"
            )
    elif name.startswith("range"):
        step, count = ("7 days", 5) if name == "range_week" else ("1 day", 31)
        for i in range(count):
            await conn.execute(
                f"CREATE TABLE {table}_{i} PARTITION OF {table} FOR VALUES"
                f" FROM (date_trunc('day', now()) - interval '{step}' * {i + 1} + interval '1 day')"
                f" TO (date_trunc('day', now()) - interval '{step}' * {i} + interval '1 day')"
            )
    await conn.execute(
        f"INSERT INTO {table} SELECT user_id, post_id, created_at FROM likes"
        f" WHERE user_id <= {SAMPLE // 2} AND created_at > now() - interval '30 days'"
    )
    await conn.execute(f"VACUUM ANALYZE {table}")


async def e2_partitioning(pool) -> dict:
    out = {}
    rng = random.Random(2)
    async with pool.acquire() as conn:
        await conn.execute("SET maintenance_work_mem = '256MB'")
        for name in LAYOUTS:
            await make_layout(conn, name)
    for name in LAYOUTS:
        table = f"exp_{name}"
        # OUR query: liked_by_me for one user and 20 posts.
        q = await run(
            pool,
            lambda c, i, t=table: c.fetch(
                f"SELECT post_id FROM {t} WHERE user_id = $1 AND post_id = ANY($2::bigint[])",
                rng.randint(1, SAMPLE // 2),
                [rng.randint(1, 100_000) for _ in range(20)],
            ),
            10_000,
        )
        async with pool.acquire() as conn:
            # How many tables (partitions) does our query touch? Count them in the plan.
            plan = await conn.fetchval(
                f"EXPLAIN (FORMAT JSON) SELECT post_id FROM {table}"
                " WHERE user_id = 123 AND post_id = ANY('{1,2,3}')"
            )
            scanned = json.dumps(plan).count('"Relation Name"')
            size = await conn.fetchval(SIZE_SQL, table)
            # Remove the oldest week of data: DELETE (rows one by one) vs DROP / DETACH a partition.
            old = "created_at < date_trunc('day', now()) - interval '27 days'"
            w0 = await wal(conn)
            if name == "range_week":
                removed_ms = await timed(conn, f"DROP TABLE {table}_4")  # the oldest week
                how = "DROP PARTITION"
            elif name == "range_day":
                removed_ms = 0.0
                for i in range(27, 31):  # the oldest 4 days
                    removed_ms += await timed(conn, f"DROP TABLE {table}_{i}")
                how = "DROP PARTITION x4"
            else:
                removed_ms = await timed(conn, f"DELETE FROM {table} WHERE {old}")
                how = "DELETE"
            wal_mb = (await wal(conn) - w0) / 1024 / 1024
        out[name] = {
            "liked_by_me": q,
            "tables_scanned": scanned,
            "size": size,
            "remove_old": {"how": how, "ms": removed_ms, "wal_mb": wal_mb},
        }
    async with pool.acquire() as conn:
        for name in LAYOUTS:
            await conn.execute(f"DROP TABLE IF EXISTS exp_{name}")
    return out


# ---------- E3 ----------


async def e3_flag_vs_watermark(pool) -> dict:
    out = {}
    rng = random.Random(3)

    async def upserts(conn, label: str) -> dict:
        st0 = await conn.fetchrow(
            "SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE relname = 'post_counters'"
        )
        w0 = await wal(conn)
        lat = []
        for b in range(50):
            ids = sorted(rng.sample(range(1, 100_001), 1000))
            likes = [rng.randint(0, 500) for _ in ids]
            started = time.perf_counter()
            await conn.execute(UPSERT_UNNEST, ids, likes, [x * 30 + b for x in likes])
            lat.append((time.perf_counter() - started) * 1000)
        w1 = await wal(conn)
        await asyncio.sleep(1)
        st1 = await conn.fetchrow(
            "SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE relname = 'post_counters'"
        )
        upd = st1["n_tup_upd"] - st0["n_tup_upd"]
        return {
            **ms_stats(lat),
            "wal_kb_per_batch": (w1 - w0) / 50 / 1024,
            "hot_update_pct": 100 * (st1["n_tup_hot_upd"] - st0["n_tup_hot_upd"]) / upd if upd else 0,
        }

    async with pool.acquire() as conn:
        await conn.execute("SET statement_timeout = 0")
        # Watermark WITHOUT an index: scan all 50M rows every 5 s.
        out["watermark_query_no_index_ms"] = await timed(
            conn, "SELECT count(*) FROM post_counters WHERE updated_at > now() - interval '5 seconds'"
        )
        out["upsert_flag_no_index"] = await upserts(conn, "flag")
        out["index_build_ms"] = await timed(conn, "CREATE INDEX exp_updated_at ON post_counters (updated_at)")
        out["watermark_query_with_index_ms"] = await timed(
            conn, "SELECT count(*) FROM post_counters WHERE updated_at > now() - interval '5 seconds'"
        )
        out["upsert_with_updated_at_index"] = await upserts(conn, "watermark")
        await conn.execute("DROP INDEX exp_updated_at")
    return out


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=16, max_size=16, command_timeout=600)
    result = {}
    for name, fn in (("E1", e1_index_cost), ("E2", e2_partitioning), ("E3", e3_flag_vs_watermark)):
        started = time.time()
        result[name] = await fn(pool)
        print(f"{name} done in {time.time() - started:.0f} s")
    await pool.close()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "experiments.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
