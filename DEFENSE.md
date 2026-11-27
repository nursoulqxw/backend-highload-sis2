# Defense notes — T7 Like & View Counter (SIS #2 and SIS #3)

- SIS #2 (protocol, 20K increments/s): this file, part 1. Details: [README.md](README.md).
- SIS #3 (database tuning): this file, part 2. Details: [SIS3.md](SIS3.md).
  A Russian version of part 2: [DEFENSE_SIS3_RU.md](DEFENSE_SIS3_RU.md).

---

# Part 1. SIS #2 — protocol implementation

## 3-minute speech

> **1. The task (20 s).**
> My system is T7, a like and view counter: about 20 thousand increments per second, 50 million
> posts, eventual consistency of at most 2 seconds. In SIS #1 I chose gRPC for internal calls.
> In SIS #2 I implemented it with idempotency, timeouts and retries, measured it against
> REST/JSON, and showed that it takes 20 thousand increments per second.
>
> **2. The design (40 s).**
> One core module, `app/core`, has all the logic. REST (FastAPI) and gRPC (grpcio) are thin
> layers on top of it, so in the benchmark the only difference is the protocol.
> Live counters are in Redis. Likes are in Postgres, because the primary key `(user_id, post_id)`
> gives us dedupe for free. A sync worker copies the counters from Redis to Postgres every 5 s.
>
> **3. Idempotency, timeouts, retries (40 s).**
> Every write is idempotent: a like is `INSERT ... ON CONFLICT DO NOTHING`, a view is Redis
> `SET NX` with a 1-hour TTL. So retries are safe. The client retries at most 3 times, only on
> UNAVAILABLE or 503, with exponential backoff and jitter, never after the 0.5 s deadline. The
> server gives Postgres and Redis 200 ms each, then answers "try again later". 59 tests, and the
> reliability tests run for both protocols.
>
> **4. How I reached 20K (40 s).**
> One Python process gave only about 2,000 requests per second: Python runs one thread of code at
> a time, so one process is one CPU core. 20,000 one-by-one calls would need about 9 cores. So:
> each view is one Lua script in Redis (one round trip instead of three), each server runs 4
> processes, and the API Gateway sends views in batches of 100. 20 thousand views per second become
> 200 calls per second. Likes stay one by one, because a user waits for the answer.
>
> **5. Results (30 s).**
> With k6 at a fixed rate: 20,001 increments per second for 60 s, 0 errors, p99 13 ms, and a new
> view is visible in a read after 15 ms at p99, against the 2 s requirement. At 20K both protocols
> keep up; at 40K gRPC degrades less, p99 105 against 196 ms. A batch of 100 views is 3.2 times
> smaller in Protobuf.
>
> **6. Conclusion (10 s).**
> The biggest win for 20K was batching, then the protocol. gRPC inside the system, REST outside
> for browsers and mobile apps.

## How to show it live (if asked)

```bash
docker compose up -d --build                      # REST + gRPC (4 processes each), Postgres, Redis, worker
curl -X PUT -H "X-User-Id: 1" localhost:8000/api/v1/posts/42/like
curl -X PUT -H "X-User-Id: 1" localhost:8000/api/v1/posts/42/like   # again: still 1 like
curl -H "X-User-Id: 1" "localhost:8000/api/v1/counters?post_ids=42"
curl -X POST -H "Content-Type: application/json" localhost:8000/api/v1/views:batch \
     -d '{"views": [{"user_id": 2, "post_id": 42}, {"user_id": 2, "post_id": 42}]}'   # counted: 1
pytest tests/test_reliability.py tests/test_batch.py -v   # idempotency, retries, batch, both protocols
```

Then open `benchmark/load_results.md` (20K) and `benchmark/results.md` (one call at a time).

## Map: where is each thing in the code?

| Topic | File | Function / place |
|---|---|---|
| Like / unlike logic | `app/core/likes.py` | `like_post`, `unlike_post` |
| View logic | `app/core/views.py` | `record_view`, `record_views` (batch) |
| Lua scripts: +1 / view / batch of views, cold key | `app/core/cache.py` | `INCR_SCRIPT`, `VIEW_SCRIPT`, `VIEW_BATCH_SCRIPT`, `load_from_db` |
| Get counters (cache-aside) | `app/core/counters.py` | `get_counters` |
| gRPC contract | `proto/counter.proto` | `service CounterService` (5 methods) |
| gRPC server, N processes | `app/grpc_api/server.py` | `CounterService`, `main` (spawn + `SO_REUSEPORT`) |
| REST server | `app/rest/routes.py`, `app/rest/main.py` | 5 routes, 503 handler |
| Server timeouts (200 ms) | `app/core/resources.py` | `command_timeout`, `socket_timeout` |
| 503 / UNAVAILABLE mapping | `app/core/errors.py`, `app/rest/main.py`, `app/grpc_api/server.py` | `BACKEND_ERRORS`, `backend_call` |
| gRPC retries + deadline | `clients/grpc_client.py` | `SERVICE_CONFIG`, `timeout=0.5` |
| REST retries + deadline | `clients/rest_client.py` | `send`, `backoff_seconds` |
| Fault injection | `app/core/faults.py` | `FaultInjector.around_call` |
| 20K load test | `loadtest/increments.js`, `benchmark/run_load.py` | k6 fixed rate, `run_matrix` |
| One-call benchmark | `benchmark/run_benchmark.py` | `run_load` |
| Metrics (all processes) | `app/core/metrics.py` | `track_request`, `render_metrics` |

## Likely questions with short answers

**1. Why gRPC and not REST?**
Inside the system we need small messages and speed. Protobuf is binary: a batch of 100 views is
1,293 B against 4,182 B of JSON (3.2x). HTTP/2 sends many calls over one connection. The `.proto`
file is a strict contract, and deadlines and retries are built in. One call at a time, gRPC was
2–2.8x faster at p50 and 8–13x at p99.

**2. How did you reach 20,000 increments per second?**
Three steps, each measured. (1) A Lua script: a view is one Redis round trip instead of three.
(2) 4 processes per server, because one Python process uses one CPU core. (3) Batching: the
gateway sends 100 views per call, so 20K views/s are 200 calls/s. Result: 20,001/s, 0 errors,
p99 13 ms, and the gRPC server used about a quarter of its CPU.

**3. Why not just more processes, without batching?**
One by one, Python handled about 2,000–2,300 calls/s per core (the gRPC runtime itself took most
of the time in the profile). 20,000 / 2,300 ≈ 9 cores for the server alone; the laptop has 8 for
everything. Batching makes the per-call cost 100 times rarer. Real systems (view counters at
YouTube scale) also count views in batches.

**4. Is batching cheating? The requirement says 20K RPS.**
The requirement is 20K *increments* per second. Batching is a design choice, not a trick: views
are fire-and-forget events, and the gateway already sees them all. Each view is still deduped and
counted one by one inside the Lua script. Likes stay unary, because the user waits for the answer.

**5. Why a Lua script in Redis?**
One network round trip instead of three, and Redis runs a script atomically: nothing can happen
between "check", "dedupe" and "+1". The cold-key check comes first, before the dedupe key is set,
so if Postgres fails during a cold load, the client's retry still counts the view.

**6. Why 4 processes, and how do they share a port?**
Python's GIL runs one thread of code at a time, so one process uses at most one core. REST:
`uvicorn --workers 4`. gRPC: 4 processes listen on the same port with `SO_REUSEPORT`, and the Linux
kernel gives each new connection to one of them. Same number for REST and gRPC, so the comparison
stays fair. Prometheus metrics are added up across processes (multiprocess mode).

**7. Why k6 at a fixed rate and not "50 requests at a time"?**
"50 at a time" (closed model) slows down together with the server: if the server is slow, fewer
requests are sent, so queueing is hidden. A fixed rate (open model) keeps sending, like real users,
so p99 shows the real waiting.

**8. How do you know eventual consistency is ≤ 2 s?**
I measured it: during the 20K test, k6 sends a view of a brand-new post 10 times per second and
reads until it appears. p50 2 ms, p99 15 ms. Reads come from Redis, which is updated at once.

**9. What is Protobuf, and why is JSON bigger?**
Protobuf sends a field number and the value, numbers as varints. JSON is text and repeats every
field name for every object (`"user_id"`, `"post_id"` ... 100 times in a batch).

**10. HTTP/2 vs HTTP/1.1?**
HTTP/1.1: one request at a time per connection. HTTP/2: many streams on one connection
(multiplexing), compressed headers, binary framing.

**11. What is idempotency, and why `PUT` for a like?**
Doing a call twice gives the same result as once. A like is a state (liked or not), so `PUT`,
which HTTP defines as idempotent. A view is an event, so `POST`, made idempotent for 1 hour with
`SET NX`. A whole `RecordViews` batch may be retried: the second time it counts 0 new views.

**12. Timeouts: why is the server's 200 ms shorter than the client's 500 ms?**
The server gives up on a slow Postgres or Redis and answers "try again later" while the client
still has time for a retry. A deadline is a point in time: all retries must fit before it.

**13. Backoff with jitter, and which errors are retried?**
Wait longer before each retry (random 0–50, 0–100, max 0–200 ms) so an overloaded server is not
hit harder; random so 1,000 clients do not retry in the same instant. Only UNAVAILABLE / 502 / 503
/ 504 or a lost connection; never a bad request, never past the deadline.

**14. p50 vs p99?**
p50: half of the requests were faster — the typical user. p99: 1% were slower — the tail. At 20K/s
1% is 200 requests every second, so the SLO is on p99.

**15. What happens if Redis loses the data?**
Redis writes an append-only file, synced once per second (AOF `everysec`): at most ~1 s is lost.
Postgres has a copy in `post_counters`, at most ~5 s old, and the cold-key rule loads it back with
`HSETNX` before the next +1, so a counter never restarts from 0.

**16. When is REST still fine?**
Public APIs for browsers and mobile apps, debugging with `curl`, rare calls. At 20K with batching
REST also kept up (p99 12 ms); gRPC won on size and under 2x load.

---

# Part 2. SIS #3 — database tuning

## 2-minute speech

> **Task:** tune the database for this workload and prove every gain with numbers.
>
> **Data:** 50 million posts and 20 million likes, 5.8 GB — more than the 4 GB of memory, like a
> real database.
>
> **Indexes:** all five queries use primary keys; EXPLAIN shows Index and Index Only Scans. An extra
> index on `likes(post_id)` would make "who liked this post" 370 times faster, but every like 63%
> slower at p99, and T7 has no such query, so I do not add it.
>
> **Partitioning:** `likes` is split by hash of `user_id` into 16 parts; every query has `user_id`,
> so it reads 1 of 16. Partitions by day would read all 31 and were 2.3x slower — but for deleting
> old data, dropping a partition was 130 times faster than DELETE.
>
> **Connection pooling:** 8 processes with pools of 10 need 160 connections, and Postgres allows
> 100. PgBouncer keeps 30: same p99 as 94 direct connections. It first made things worse — 88%
> errors — I found two causes in the logs and fixed them: 0.05%.
>
> **Read replica:** reads moved off the primary, p99 of reads 173 → 50 ms; replication lag at most
> 0.6 s, inside the 2 s requirement.
>
> **Honestly:** config tuning did not speed up single queries on one laptop — the tables do not fit
> in memory anyway, and the replica shares the CPU with the primary.

## Every change: what → number → why

| Change | Number | Why |
|---|---|---|
| Sync worker: one `unnest` statement instead of `executemany` | batch p99 44 → 23 ms | 1 parse, 1 plan, 1 round trip instead of 1,000 |
| Index `likes(post_id)` (not kept) | read 488 → 1.3 ms; insert p99 +63% | every insert writes one more B-tree |
| `likes` HASH(user_id) x16 | 1 of 16 partitions per query | all queries have `user_id = $1` → partition pruning |
| DROP PARTITION vs DELETE | 9 ms vs 1,131 ms; WAL 0.06 vs 21.8 MB | DROP removes a file; DELETE touches and logs every row |
| Pool 2 / 5 / 10 / 25 / 50 | pool 2: 5–7% failed; pool 25 best | too small = queue in the app; too big = contention |
| PgBouncer | 30 connections instead of 94, same p99 | transaction pooling: a connection only for one transaction |
| asyncpg `reset` off behind PgBouncer | failed 88% → 0.05% | the reset was a second transaction = 2 PgBouncer slots per query |
| `client_connection_check_interval` | 84 "zombie" backends gone | a backend notices its client left and stops |
| Read replica | read p99 173 → 50 ms | reads no longer compete with writes |
| Flag (`dirty_posts`) instead of watermark | watermark: 2.3 s scan, or an index that drops HOT 100% → 0% | `updated_at` changes on every update |
| `fillfactor = 80` | HOT got worse, 99% → 65% | replica's `hot_standby_feedback = on` stops cleanup on the primary |
| Postgres config (cache 384 MB, SSD settings) | no measurable gain | 5.8 GB does not fit in RAM either way |

## Lecture 7's six questions (slide 22, "for SIS #3")

1. **Which wall?** Connections (160 > 100) and cold-key reads. Not writes.
2. **RPO / RTO?** Likes: RPO 0 on the primary; ≤ 0.6 s if its disk is lost (async replica). Redis ≤ 1 s.
   RTO: no automatic failover, manual promote — minutes; with Patroni, tens of seconds.
3. **Which reads tolerate lag?** Only GetCounters → replica. The cold load before a +1 and all writes → primary.
4. **Lag monitored and acted on?** Monitored (`pg_stat_replication`, p99 355 ms under overload).
   Not acted on yet — next step: route reads to the primary when lag > 1 s.
5. **Does the key prune?** Yes: EXPLAIN shows `likes_p12` only.
6. **Pods × pool fit?** Direct no (160 > 100); PgBouncer keeps 30–40. Little's Law: 600/s × 10 ms ≈ 6 busy.

## Likely questions (SIS #3)

**Why no more indexes?** Every query already uses a primary key. Each extra index costs on every
write: measured +63% p99 on a like.

**Slowest query, and why can't an index fix it?** GetCounters of 100 random posts from 50M
(p99 159 ms). It already uses the index, but 100 rows sit on ~100 pages, mostly read from disk.
The fix is not in the database: Redis keeps the live counters, Postgres is only hit for cold keys.

**Why hash by `user_id`, not by date?** Our queries filter by `user_id`, never by date. By date,
every query reads every partition.

**Why partition at all if it is not faster?** Maintenance of a table that only grows: VACUUM,
index rebuilds and ANALYZE run on 16 small parts instead of one big table.

**Why is `post_counters` not partitioned?** A read asks for 100 random `post_id`s at once — it would
touch every partition. And it has a fixed size, nothing to drop.

**Why did PgBouncer first make it worse?** (1) asyncpg reset every connection after each query —
behind PgBouncer that is a second transaction, so each request took two slots. (2) When the app
gave up on a query, the Postgres backend kept running and held a slot ("zombies"). Fixes: no reset,
`client_connection_check_interval`, `max_db_connections`.

**Is a bigger pool better?** No. Pool 2 queued requests (5–7% failed); 25 was best; direct
connections grew to 94, near the 100 limit. PgBouncer kept 30 with the same p99.

**Does the replica break consistency?** Lag ≤ 0.6 s (measured under overload), requirement ≤ 2 s.
A user may not see their own like in `liked_by_me` for that long; allowed for a counter
(Lecture 8, slide 10: eventual is "fine for likes, view counters").

**Why `synchronous_commit = off` in the sync worker?** `post_counters` is a copy; Redis has the live
values. A crash loses ~0.6 s of the copy, and the next sync rewrites it. Likes keep it on.

**Why did config tuning not help?** 5.8 GB of data, 4 GB of RAM for everything: 128 or 384 MB of
cache does not change much, and Postgres also uses the OS cache. The replica on the same laptop
takes CPU and disk from the primary.

## Show it live (SIS #3)

```bash
docker compose -f docker-compose.yml -f docker-compose.db.yml up -d
docker compose exec postgres psql -U postgres -d counters -c \
  "EXPLAIN (ANALYZE, BUFFERS) SELECT post_id FROM likes WHERE user_id = 12345 AND post_id = ANY('{1,2,3}');"
docker compose -f docker-compose.yml -f docker-compose.db.yml exec pgbouncer \
  psql -h 127.0.0.1 -p 6432 -U postgres pgbouncer -c "SHOW POOLS;"
docker compose exec postgres psql -U postgres -c "SELECT client_addr, state, replay_lag FROM pg_stat_replication;"
cat benchmark/db_results.md
```
