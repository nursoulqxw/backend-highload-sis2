# SIS #3 — Database tuning (T7 Like & View Counter)

**The task:** design the schema and indexes for the workload, analyse query plans with
EXPLAIN ANALYZE, apply partitioning, read replicas and connection pooling; submit a
before/after benchmark with p95/p99 and an explanation of every gain.

**Short answer (all numbers measured on one laptop, see "Limits" at the end):**

| Change | Result | Kept? |
|---|---|:---:|
| Sync worker: 1 statement with `unnest` instead of `executemany` (1000 statements) | p99 of a 1000-row batch **44 → 23 ms (1.9x)**, rows/s **+45%** | yes |
| Pool size 2 / 5 / 10 / 25 / 50 per process | pool 2: 5–7% failed; 25: best p99. Direct: Postgres connections grow to **94** (near the limit). PgBouncer: capped at **30**, same latency | PgBouncer; pool stays 10 (all "after" runs used 10; 25 is the next step) |
| PgBouncer (transaction pooling) | 8 processes per API: `too many clients` errors **15 → 0**; Postgres connections capped at 40 (but no extra capacity, see 5.3) | yes |
| asyncpg `reset` off behind PgBouncer | failed calls **88% → 0.05%** (a bug we found under load, see 5.2) | yes |
| Read replica for GetCounters | p99 of reads with 8 processes **173 → 50 ms**; replication lag p50 1 ms, max 0.6 s (< 2 s) | yes |
| `likes` partitioned by HASH(user_id) x16 | every query reads **1 of 16** partitions; latency about the same | yes |
| Remove old data: DROP PARTITION vs DELETE | **9 ms vs 1,131 ms (130x)**, WAL **0.06 MB vs 21.8 MB** | (pattern) |
| Watermark (`updated_at` index) vs flag (`dirty_posts`) | without an index the watermark scan takes **2.3 s**; with it, HOT updates **100% → 0%** and WAL **+45%** | flag |
| Extra index `likes(post_id)` | "who liked post X" **488 → 1.3 ms p50 (~370x)**; but every like INSERT p99 **+63%**, throughput **−9%**, +304 MB; no T7 query needs it | no |
| Bigger `shared_buffers`, SSD planner settings | no measurable gain: the tables (5.8 GB) do not fit in RAM anyway | yes (harmless) |

---

## 1. The workload and the data

T7 sends only 5 kinds of SQL (code: `app/core`, `app/worker`):

| # | Query | When | Rate at 20K increments/s |
|---|---|---|---|
| Q1 | `INSERT INTO likes ... ON CONFLICT DO NOTHING` | like | ~1,000/s |
| Q2 | `DELETE FROM likes WHERE user_id = $1 AND post_id = $2` | unlike | rare |
| Q3 | `SELECT post_id, likes, views FROM post_counters WHERE post_id = ANY($1)` | cold key (post not in Redis) | ~10% of increments |
| Q4 | `SELECT post_id FROM likes WHERE user_id = $1 AND post_id = ANY($2)` | `liked_by_me` in GetCounters | every read |
| Q5 | upsert of up to 1000 rows into `post_counters` | sync worker, every 5 s | thousands of rows / 5 s |

Views never touch Postgres (Redis only), so Postgres sees a small part of the 20K/s.

**Data** ([db/seed.sql](db/seed.sql)): **50M posts** (`post_counters`, 4.2 GB) and
**20M likes** (`likes`, 1.6 GB). 10M users with 2 likes each: one to a hot post (100K hot posts),
one to any post. Together 5.8 GB — more than the 4 GB RAM of the Docker VM, so the
database cannot keep everything in memory, like a real one.

## 2. Indexes: why there is no extra index

Every query is answered by a **primary key**:

| Query | Index used (EXPLAIN) | Why it fits |
|---|---|---|
| Q1, Q2 like / unlike | `likes` PK `(user_id, post_id)` | exact key |
| Q3 cold read | `post_counters` PK `(post_id)` | `post_id = ANY(...)` → Index Scan |
| Q4 liked_by_me | `likes` PK `(user_id, post_id)` | **Index Only Scan**: `user_id` is the first column, `post_id` is in the index too, the table is not read |
| Q5 upsert | `post_counters` PK | `ON CONFLICT (post_id)` needs exactly this index |

The full plans: [benchmark/db/explain_before.txt](benchmark/db/explain_before.txt),
[benchmark/db/explain_after.txt](benchmark/db/explain_after.txt).

**The cost of an index** ([benchmark/db_experiments.py](benchmark/db_experiments.py), E1): an index
`likes(post_id)` ("who liked this post") makes every like more expensive: INSERT p99
**2.1 → 3.5 ms (+63%)**, throughput **16,361 → 14,906/s (−9%)**, because each INSERT now updates
two B-trees instead of one (and the second one is in random `post_id` order).
T7 never asks "who liked post X", so we do not add it.

**What the same index would buy** ([benchmark/db_index.py](benchmark/db_index.py)): the query
`SELECT user_id FROM likes WHERE post_id = $1 LIMIT 20` ("who liked post X"):

| | p50 | p95 | p99 |
|---|---:|---:|---:|
| without an index on `post_id` | 488 ms | 667 ms | 713 ms |
| with `likes(post_id)` (304 MB, built in 10 s) | **1.3 ms** | 10.7 ms | 17.8 ms |

Without it, EXPLAIN ([benchmark/db/explain_index.txt](benchmark/db/explain_index.txt)) shows an
`Index Only Scan` over the **whole** primary key `(user_id, post_id)` of all 16 partitions:
`Buffers: read=78786` (~615 MB) per query. The key contains `post_id`, but as the **second** column,
so it cannot be used to jump to one post (the leftmost-prefix rule, Lecture 6, slide 7).

So the decision is a trade: ~370x faster for a read T7 does not have, against +63% p99 on a write
T7 does ~1,000 times per second. If a "likers" list is added later, add this index then.

**The slowest query: Q3 with 100 posts** (GetCounters after a Redis restart):
p50 74 ms, p99 159 ms. EXPLAIN: `Index Scan ... Buffers: shared hit=230 read=273` — 100 random
rows of a 4.2 GB table sit on ~100 different pages, most of them must be read from disk. An
index cannot help: it **is** already an index lookup. What helps is not asking Postgres: Redis
keeps the live counters (cache-aside), and this query happens only for cold keys.

## 3. Before / after: the real SQL (direct, 16 connections)

Script: [benchmark/db_bench.py](benchmark/db_bench.py). Before = schema 0001 + default config.
After = migration 0002 + tuned config + a running replica.

| Query | before p95 | before p99 | after p95 | after p99 |
|---|---:|---:|---:|---:|
| Q1 like | 1.52 | 2.59 | 1.62 | 2.67 |
| Q3 cold read x1 | 3.78 | 7.97 | 2.95 | **5.80** |
| Q3 cold read x100 | 116.15 | 158.92 | 122.02 | 241.37 |
| Q4 liked_by_me x20 | 1.83 | 3.27 | 3.15 | 6.12 |
| Q5 sync 1000 rows, `executemany` | 20.42 | 44.49 | 28.76 | 127.42 |
| Q5 sync 1000 rows, **`unnest`** | **14.68** | **22.74** | 17.17 | 24.77 |

(ms)

**What these numbers say — honestly:**

1. **`unnest` vs `executemany` is the clear win** (same database, same moment): p99 44 → 23 ms,
   WAL 233 → 190 KB per batch. One statement = one parse, one plan, one round trip; 1000
   statements = 1000 of each. The sync worker now uses `unnest` ([app/worker/sync.py](app/worker/sync.py)).
2. **The config tuning did not make single queries faster.** Q3 x1 is a bit better, the rest
   is the same or worse. Why:
   - the tables (5.8 GB) do not fit in RAM in both cases, so 128 MB vs 384 MB of
     `shared_buffers` changes little (the cache hit ratio stayed ~97%); Postgres also uses
     the OS page cache, so a small `shared_buffers` is not a disaster;
   - "after" runs a **second Postgres (the replica) on the same laptop**: it replays every
     write and takes CPU and disk from the primary. On real hardware the replica is another machine.
3. **HOT updates got worse: 99% → 65%**, although `fillfactor = 80` should help. Reason: the
   replica has `hot_standby_feedback = on`. It tells the primary "I still need old row versions",
   so the primary cannot clean them from the page, the free space is not freed, and HOT fails.
   This is a real trade-off:
   - `on`: long reads on the replica are never cancelled, but the primary keeps dead rows (bloat);
   - `off`: the primary stays clean, but a long read on the replica can fail with
     "canceling statement due to conflict with recovery".
   Our replica reads are tiny (< 10 ms), so `off` would be the better choice for T7.

## 4. Partitioning

### 4.1 Which partitioning? ([benchmark/db_experiments.py](benchmark/db_experiments.py), E2)

Same 5M-row copy of `likes`, our query Q4 (`liked_by_me`), 4 layouts:

| Layout | partitions the query reads | p50 ms | p95 ms | p99 ms | size |
|---|---:|---:|---:|---:|---:|
| no partitions | 1 (the table) | 0.71 | 1.81 | 4.93 | 444 MB |
| **HASH(user_id) x16** | **1** | 1.00 | 2.11 | 3.77 | 400 MB |
| RANGE(created_at) per week, 5 | 5 | 0.83 | 1.65 | 2.57 | 501 MB |
| RANGE(created_at) per day, 31 | **31** | **1.93** | **4.79** | **8.11** | 503 MB |

The RANGE layouts are bigger: their primary key must contain the partition key, so it is
`(user_id, post_id, created_at)` — a wider index.

- **HASH(user_id)** wins on the plan: every T7 query on `likes` has `user_id = $1`, so Postgres
  reads only **1 of 16** partitions (partition pruning). In EXPLAIN after the migration:
  `Index Only Scan using likes_p12_pkey on likes_p12` — one partition, the other 15 are not touched.
- **RANGE by day / by week** (on `created_at`) does not prune our query: it has no date filter,
  so it must look into **every** partition (31 for days). More partitions = more work per query.
- Latency is close for all layouts at this size: a B-tree is very flat, 1 big index or
  16 small ones is a difference of ~1 page per lookup.

**So why partition at all?** Not for speed of one query, but for **maintenance** of a table that
only grows: VACUUM, index rebuilds and `ANALYZE` work per partition (16 small jobs instead of
one 1.6 GB job), and each partition's index fits in memory better.

**Why `post_counters` is NOT partitioned:** Q3 asks for 100 random `post_id`s at once — with hash
partitions it would touch all of them. And the table has a fixed size (1 row per post) and no
old data to drop.

### 4.2 Deleting old data: DELETE vs DROP PARTITION

Remove the oldest week of likes from the 5M-row copy:

| Way | time | WAL written |
|---|---:|---:|
| `DELETE FROM likes WHERE created_at < ...` (no partitions / hash partitions) | **1,131 ms** | **21.8 MB** |
| `DROP TABLE <partition of that week>` (weekly RANGE) | **9 ms** | **0.06 MB** |
| `DROP` 4 daily partitions (daily RANGE) | 12 ms | 0.04 MB |

DELETE visits every old row, writes each change to the WAL (and to the replica), and leaves dead
rows that VACUUM must clean later. DROP removes a whole file: **130x faster** and ~400x less WAL.
Daily vs weekly: daily drops are just as cheap and can remove data more precisely, but (table
above) 31 partitions make every query without a date filter 2.3x slower at p50. Weekly is the
better balance for a 30-day lifetime.

T7 keeps likes forever, so we do not need this today. But if we ever add data with a lifetime
(e.g. a "views history" table kept for 30 days), it should be **RANGE-partitioned by day or week**
and cleaned with `DROP PARTITION`: it is a metadata change (no row-by-row work, no WAL per row,
no dead rows, no VACUUM afterwards).

## 5. Connection pooling: PgBouncer

### 5.1 Why

Every Postgres connection is a whole OS process (~5–10 MB, plus a fork on connect).
`max_connections = 100`. With 8 processes per API: 8 x 2 APIs x 10 (pool) = **160 > 100**.

PgBouncer ([db/pgbouncer/pgbouncer.ini](db/pgbouncer/pgbouncer.ini)) in **transaction mode**:
the APIs open as many connections to PgBouncer as they want; PgBouncer gives a real Postgres
connection only for the time of one transaction, and keeps **at most 40** per database.

### 5.2 What went wrong first (and what we learned)

With PgBouncer, the first run had **88–100% failed calls**. Two problems, found in the logs:

1. **asyncpg resets every connection on release** (`RESET ALL; CLOSE ALL; UNLISTEN *`). Behind
   PgBouncer that is one more transaction per query → every request needs **2** PgBouncer slots
   → slots run out → the reset times out (200 ms) → the connection is closed and opened again →
   an avalanche. Fix: no reset behind PgBouncer (`reset=_no_reset` in
   [app/core/resources.py](app/core/resources.py)); safe because we keep no session state.
2. **"Zombie" backends.** When the client gives up, PgBouncer closes its server connection, but
   the Postgres backend keeps running (or waiting) and holds one of the 100 slots. We saw **84**
   such backends → `too many clients` for everyone. Fix: `client_connection_check_interval = 250ms`
   in Postgres (the backend notices that its client is gone and stops) and `max_db_connections = 40`
   in PgBouncer (a hard cap).

After the fixes, at 300 likes/s + 300 reads/s, 8 processes per API: **17 failed of 35,912 (0.05%)**.

### 5.3 Direct vs PgBouncer under high load ([db/demo_pgbouncer.sh](db/demo_pgbouncer.sh))

The same overload (1000 likes/s + 1500 reads/s, 30 s), 8 processes per API, schema 0001,
default config — only the connection path is different:

| | failed calls | `too many clients` errors in the log |
|---|---:|---:|
| direct connections | 24.7% | **15** |
| through PgBouncer | 43.6% | **0** |

PgBouncer **removed the connection-limit errors**, but it did **not add capacity**: this load is
~5x what the laptop can serve, so both setups are overloaded. With PgBouncer the requests wait in
its queue for one of the 40 connections and miss the 500 ms client deadline; without it some
requests fail at once with `too many clients` and the rest get through a bit faster.

**Lesson:** PgBouncer solves the *number of connections*, not the *speed of the database*. If the
database is too slow, the queue just moves from Postgres into PgBouncer. Its value shows at a load
the database can serve (section 5.2: 0.05% failed with 16 processes) and when you add more app
processes / pods than Postgres can have connections.

### 5.4 Pool size: 2 / 5 / 10 / 25 / 50 per process ([db/run_pool.sh](db/run_pool.sh))

The same moderate load (300 likes/s + 300 reads/s, every read a cold key, 60 s), 4 processes per
API = 8 asyncpg pools, reads from the primary. Only the pool size and the connection path change.
"Postgres conns" = the most client backends seen in `pg_stat_activity` (sampled every second).

| Path | pool / process | Postgres conns | failed | like p95 | like p99 | read p95 | read p99 |
|---|---:|---:|---:|---:|---:|---:|---:|
| direct | 2 | 18 | 4.63% | 176 | 435 | 195 | 440 |
| direct | 5 | 30 | 0.00% | 53 | 151 | 82 | 195 |
| direct | 10 | 50 | 1.92% | 126 | 400 | 172 | 394 |
| direct | **25** | **94** | 0.12% | 19 | 108 | 47 | 176 |
| direct | 50 | 89 (limit) | 0.33% | 74 | 190 | 133 | 255 |
| PgBouncer | 2 | 9 | 6.87% | 268 | 474 | 278 | 480 |
| PgBouncer | 5 | 21 | 0.57% | 98 | 254 | 157 | 364 |
| PgBouncer | 10 | 30 | 0.39% | 61 | 255 | 89 | 277 |
| PgBouncer | **25** | **30** | 0.08% | 16 | 111 | 44 | 167 |
| PgBouncer | 50 | 30 | 14.31% | 385 | 515 | 417 | 513 |
| PgBouncer | 50, repeated | 30 | 0.30% | 43 | 196 | 90 | 249 |

(ms) What holds across runs:

1. **Too small is bad:** pool 2 → 5–7% failed, p99 ~450 ms in both paths. Requests wait in the
   app for a free connection and hit the 200 ms query timeout.
2. **Direct: Postgres connections grow with the pool** — 18, 30, 50, 94 — and then stop at the
   `max_connections = 100` wall (pool 50: 89; the pools cannot grow further). One more API process
   or a bigger pool and new connections are refused.
3. **PgBouncer: Postgres connections stay at 30** (`default_pool_size`) whatever the app pool is.
4. **Same speed, a third of the connections:** at pool 25, direct p99 108 / 176 ms with **94**
   connections; PgBouncer 111 / 167 ms with **30**. That is the whole point of a pooler:
   the app may open many connections, the database serves a few busy ones.
5. **Little's Law agrees:** 600 queries/s x ~10 ms ≈ 6 busy connections on average; 30 leaves room
   for bursts. Hundreds would only add contention (Lecture 7, slide 20: "smaller is usually faster").

**Noise:** the curve is not smooth (direct pool 10 is worse than pool 5) and one run (PgBouncer, 50)
had 14% failed calls but 0.3% when repeated. On one laptop, a single run can be disturbed; we trust
the trends that repeat (points 1–4), not one number. Under a 2x higher load (500 + 500/s) every pool
size was overloaded (13–25% failed, [benchmark/db/pool_overload/](benchmark/db/pool_overload/)),
so pool size cannot fix a database that is simply too busy.

## 6. Read replica

Streaming replication ([docker-compose.db.yml](docker-compose.db.yml), [db/replica/entrypoint.sh](db/replica/entrypoint.sh)):
the replica copies the primary with `pg_basebackup` and then replays its WAL. GetCounters
(Q3 + Q4, read-only) goes to the replica; all writes go to the primary.

End-to-end, k6 through the gRPC API, 300 likes/s + 300 reads/s, every read a cold key,
8 processes per API, PgBouncer in both runs:

| | failed | like p95 | like p99 | read p95 | read p99 |
|---|---:|---:|---:|---:|---:|
| reads from the primary (no replica) | 0.20% | 6.9 | 136.7 | 24.5 | 172.9 |
| **reads from the replica** | **0.05%** | **4.4** | **33.1** | **17.4** | **49.6** |

The tail (p99) is **3–4x better**: reads no longer compete with writes for the primary.

**Replication lag**, sampled once per second **during an overload test**: p50 **1 ms**,
p99 355 ms, max **610 ms**. The requirement is eventual consistency ≤ 2 s — kept even under
overload. Read-your-writes: a user who just liked a post may, for up to that lag, see
`liked_by_me = false` from the replica. For T7 this is allowed (≤ 2 s).

## 7. How the sync worker finds changed posts: flag vs watermark

The sync worker must find "posts that changed in the last 5 s" among 50M. Two ways:

- **Watermark**: `SELECT ... WHERE updated_at > last_sync_time`.
- **Flag**: mark the post as dirty when it changes. We use the Redis set `dirty_posts`
  (`SADD` on every change, `SPOP 1000` in the worker).

Measured on the 50M-row `post_counters` ([benchmark/db_experiments.py](benchmark/db_experiments.py), E3):

| | watermark, no index | watermark + index on `updated_at` | flag (`dirty_posts`), no index |
|---|---:|---:|---:|
| find changed posts | **2,285 ms** (Seq Scan of 50M rows) | 9 ms | `SPOP` in Redis, < 1 ms |
| upsert of 1000 rows, p50 | — | 14.8 ms | 14.3 ms |
| WAL per batch | — | **686 KB** | **474 KB** |
| HOT updates | — | **0%** | **100%** |

So the watermark is slow without an index, and **with** an index it breaks HOT updates on our
hottest table (`updated_at` changes on **every** update, so every update must also update the
index). The flag costs one `SADD` in Redis, which we already do in the same Lua script.

## 8. Before / after end-to-end (the API, not only SQL)

k6 ([loadtest/db_mix.js](loadtest/db_mix.js)) through the gRPC API: 300 likes/s (Postgres INSERT)
+ 300 reads/s (GetCounters of 20 long-tail posts → cold keys → Q3 + Q4 on Postgres), 60 s.

| Setup | processes | failed | like p95 | like p99 | read p95 | read p99 |
|---|---:|---:|---:|---:|---:|---:|
| before: 0001, default config, direct | 4 | 0.05% | 5.9 | 30.7 | 23.3 | 55.8 |
| before | 8 | 0.09% | 4.2 | 19.2 | 15.0 | 43.7 |
| after: 0002, tuned, PgBouncer, replica | 4 | 0.40% | 7.0 | 181.9 | 20.2 | 213.6 |
| after | 8 | 0.05% | 4.4 | 33.1 | 17.4 | 49.6 |
| after, no replica | 8 | 0.20% | 6.9 | 136.7 | 24.5 | 172.9 |

(ms) **Honest reading:** at this moderate load, before and after are about the same. One
laptop cannot show the gains of a second machine (the replica) or of a config built for more
RAM. The "after, 4" run was the first one after restarting all containers with cold caches; it
has the worst tail. The real, measured gains of SIS #3 are: the `unnest` batch (1.9x), the
replica for the read tail (3–4x vs the same setup without it), and PgBouncer under high
concurrency (section 5).

## 8b. The six questions for a scaled database (Lecture 7, slide 22)

**1. Which wall are we at?** *Connections* first: 8 processes x 2 APIs x 10 = 160 > `max_connections`
100 (section 5). Then *reads* of cold keys (Q3 x100 is the slowest query, section 2). Not *writes*:
views never reach Postgres, likes are ~1,000/s, and the sync worker writes in batches.

**2. RPO and RTO** (how much data we may lose / how long writes stop):

| Data | Where | RPO | Why |
|---|---|---|---|
| likes | Postgres primary, `synchronous_commit = on` | **0** on a primary crash | COMMIT waits for the WAL on disk |
| likes, if the primary's disk is lost | async replica | the replication lag, **≤ 0.6 s** measured | async replication |
| `post_counters` (copy) | Postgres, `synchronous_commit = off` in the sync worker | ~0.6 s of the copy | Redis still has the values; the next sync rewrites them |
| live counters | Redis, AOF `everysec` | **≤ 1 s** of increments | fsync once per second |

RTO: there is **no automatic failover** here (no Patroni / etcd). Promoting the replica is manual
(`pg_ctl promote`, then point PgBouncer's `counters` to it), so minutes, not seconds. With Patroni
it would be tens of seconds (Lecture 7, slide 11). Reads keep working from Redis during that time.

**3. Which reads tolerate lag?** Listed explicitly:

| Read | Goes to | Why |
|---|---|---|
| GetCounters: cold-key load of `post_counters` + `liked_by_me` | **replica** | eventual consistency ≤ 2 s is the requirement; lag p99 355 ms |
| cold-key load **before an increment** (`+1` on a post missing in Redis) | **primary** | a stale base value + 1 would be written into Redis and later synced back |
| everything that writes (like, unlike, sync worker) | **primary** | a replica is read-only |

Read-your-writes: right after a like, `liked_by_me` from the replica may be `false` for up to the lag.
Allowed by the 2 s requirement; the cheap fix, if needed, is "read from the primary for a few seconds
after this user writes" (Lecture 7, slide 9).

**4. Is lag monitored and acted on?** *Monitored*: sampled once per second under load (section 6),
and visible any time in `pg_stat_replication.replay_lag`. *Acted on*: **not yet** — the app does not
switch reads back to the primary when the lag grows. Next step: a background check of
`now() - pg_last_xact_replay_timestamp()` on the replica; above 1 s, route GetCounters to the primary.

**5. Does the partition key prune?** Yes. Every query on `likes` has `user_id = $1`; EXPLAIN after the
migration shows `Index Only Scan using likes_p12_pkey on likes_p12` — 1 of 16 partitions.

**6. Do pods × pool fit?** Without PgBouncer: no (160 > 100, and we saw `too many clients`). With it:
the apps may open up to 2,000 client connections, PgBouncer keeps at most **40** server connections
per database (`max_db_connections`), so primary 40 + sync/admin < 100. By Little's Law the need is
small anyway: 600 queries/s x ~10 ms = **~6 busy connections**.

## 9. How to reproduce

```bash
sh db/run_sis3.sh          # ~1 h: seed 50M rows, BEFORE, migrate, AFTER (overload run), experiments
sh db/run_mix.sh           # ~20 min: before / after end-to-end at a moderate load (section 8)
sh db/demo_pgbouncer.sh    # ~4 min: direct vs PgBouncer under high load (section 5.3)
sh db/run_pool.sh          # ~15 min: pool size 2/5/10/25/50, direct vs PgBouncer (section 5.4)
python -m benchmark.db_index   # ~3 min: what an index on likes(post_id) buys on reads (section 2)
python -m benchmark.db_bench --report      # -> benchmark/db_results.md
```

Files: [db/](db/) (seed, EXPLAIN script, Postgres config, PgBouncer, replica),
[migrations/versions/0002_tuning.py](migrations/versions/0002_tuning.py),
[benchmark/db_bench.py](benchmark/db_bench.py), [benchmark/db_experiments.py](benchmark/db_experiments.py),
raw results in [benchmark/db/](benchmark/db/).

## 10. Limits

- Everything runs on **one laptop** (Apple M2, Docker VM with 8 CPUs / 4 GB): k6, 16 API processes,
  Redis, PgBouncer, **two** Postgres. The replica and the primary share the same CPU and disk.
- The first after-run at 1000 likes/s + 1500 reads/s overloaded **both** setups (96–100% failed),
  so it is not used for comparison; it is where we found the PgBouncer problems in 5.2.
- p99 changes noticeably between runs on a laptop; we compare runs done the same way, close in time.
- **The replica broke once, and that is a lesson too.** While we migrated back and forth, the primary
  ran with the default `wal_keep_size = 0`, wrote GBs of WAL and deleted old segments. The replica
  then failed with `requested WAL segment ... has already been removed` and had to be copied again.
  In production you use a **replication slot**: the primary keeps WAL until the replica has it —
  but if the replica is dead for long, that WAL can fill the primary's disk, so the slot must be
  monitored (or limited with `max_slot_wal_keep_size`).
- The experiments (E3) write random values into `post_counters` of hot posts, so after them the
  counters no longer match the `likes` table. Fine for a benchmark database; reseed for demos.

## AI use

Claude (Anthropic) was used to write code and scripts, run the benchmarks and draft this report.
Every number comes from a measurement in [benchmark/db/](benchmark/db/); the design decisions and
their trade-offs were reviewed and explained by the author.
