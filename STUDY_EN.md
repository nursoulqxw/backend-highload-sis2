# Understand the project in 30 minutes (study notes)

A plain-words explanation. Full details: [README.md](README.md) (SIS #2), [SIS3.md](SIS3.md) (SIS #3).
Defense cheat sheets: [DEFENSE.md](DEFENSE.md), [DEFENSE_SIS3_RU.md](DEFENSE_SIS3_RU.md).
Russian version of this file: [STUDY_RU.md](STUDY_RU.md).

---

## 0. The project in one sentence

**T7 is a like and view counter service for posts** (like under an Instagram post:
❤️ 1,234, 👁 56,789). It must handle **20,000 actions per second**, store **50M posts**,
and a new like must be visible **within 2 seconds**.

- **SIS #2**: *how* clients talk to the service (gRPC vs REST), and nothing breaks on retries and
  timeouts. Plus reaching 20K per second.
- **SIS #3**: make the **database** (Postgres) fast and prove it with before/after measurements.

---

## 1. The big picture (remember this diagram)

```
                  client (app)
                        │  gRPC or REST
                        ▼
          ┌───────── API (4 processes) ────────┐
          │                                     │
   +1 / −1 ▼                              read  ▼
      ┌────────┐   at once         ┌──────────────┐
      │ Redis  │ ◄──────────────── │ GetCounters  │
      │(memory)│                   └──────────────┘
      └────────┘                          │ if not in Redis
          │ every 5 seconds               ▼
          ▼                        ┌──────────────┐
   ┌─────────────┐                 │ Postgres     │
   │ sync-worker │ ─► PgBouncer ─► │ primary      │ ──copy──► replica (read only)
   └─────────────┘                 └──────────────┘
```

**Main idea:** Redis is a "whiteboard" (fast, in memory), Postgres is a "logbook"
(safe, on disk).

- Every +1 goes to **Redis**: microseconds.
- The **sync-worker** copies the changes from Redis to Postgres every 5 seconds.
- Reads also come from **Redis**. So a new like is visible at once (15 ms for us), not after 5 s.

Example: a user likes post 42 → in Redis `post:42 likes` becomes 101 → the next
`GetCounters` sees 101 at once. Within 5 s the worker writes 101 to Postgres "just in case".

---

## 2. Where the data lives

| Where | What | Why |
|---|---|---|
| Postgres `likes(user_id, post_id)` | Who liked what | Key `(user_id, post_id)` → one user likes a post **only once** |
| Postgres `post_counters(post_id, likes, views)` | Copy of the counters on disk | If Redis dies, we recover from here |
| Redis `post:{id}` | `{likes, views}` | The live counter, reads come from here |
| Redis `seen:{user}:{post}` | "seen", lives 1 hour | So one user cannot inflate views |
| Redis `dirty_posts` | Set of changed posts | So the worker knows what to write to Postgres |

---

# PART 1. SIS #2: protocol and reliability

## 3. Five operations

| Operation | REST | gRPC |
|---|---|---|
| Like | `PUT /api/v1/posts/{id}/like` | `LikePost` |
| Unlike | `DELETE /api/v1/posts/{id}/like` | `UnlikePost` |
| View | `POST /api/v1/posts/{id}/view` | `RecordView` |
| Many views at once | `POST /api/v1/views:batch` | `RecordViews` |
| Read counters | `GET /api/v1/counters?post_ids=1,2,3` | `GetCounters` |

One logic for both protocols: [app/core/](app/core/). REST ([app/rest/](app/rest/)) and
gRPC ([app/grpc_api/](app/grpc_api/)) are just two "doors" into the same house.

## 4. Why gRPC

- **Protobuf is small.** The answer for 100 posts: JSON 6,281 bytes, Protobuf 1,098 bytes (5.7× smaller).
  It sends field numbers (`1`) instead of field names (`"likes":`).
- **HTTP/2**: many requests at the same time over one connection.
- **Strict contract**: [proto/counter.proto](proto/counter.proto). Client and server code is
  generated from one file, so they cannot disagree.
- **Where to use it:** inside the system (between services). Outside (browsers): REST/JSON.

## 5. Idempotency (the most common question!)

**Idempotent** = doing it 2 times gives the same result as 1 time.

Why: the server saved the like, but the answer was lost on the network. The client does not know
if it was saved, so it sends it again. If a like were "+1 every time", we would count 2. **Wrong.**

How we do it:
- **Like:** `INSERT ... ON CONFLICT DO NOTHING`. The second time inserts 0 rows → the counter does not change.
- **View:** `SET seen:7:42 NX EX 3600` (NX = "only if the key does not exist"). The second time within
  an hour does nothing.
- **Read:** only reads, always idempotent.

Why a like is `PUT` and a view is `POST`: a like is a **state** ("liked: yes"), and `PUT` =
"set the state", idempotent by the HTTP standard. A view is an **event**, so `POST`, and we make it
idempotent ourselves with `SET NX`.

## 6. Timeouts and retries

- **Client deadline 0.5 s** for the whole call, retries included. Nothing waits forever.
- **Server → Postgres/Redis 200 ms.** Too slow → the server answers "try again later"
  (REST 503, gRPC UNAVAILABLE). Server timeout < client deadline → the client has time to retry.
- **Retries:** at most 3 attempts, only on "try again later", never on "bad request" (422).
- **Backoff + jitter:** before a retry, wait a **random** time (0–50 ms, then 0–100 ms…).
  Example: 1000 clients fail at the same moment. Without randomness they all retry at the same
  moment and take the server down again (a "retry storm"). With randomness they spread out.

## 7. How we reached 20,000 per second

At first it was ~2,000 per process. Three steps:

1. **Lua scripts in Redis** ([app/core/cache.py](app/core/cache.py)). "Check + add + mark" is
   3 Redis calls. Lua runs them **inside Redis in 1 round trip**, atomically
   (nothing can happen in between).
2. **4 processes** per server (Python uses 1 core per process → we use 4 cores).
3. **Batching:** `RecordViews` sends 100 views in one request. Like carrying 100 letters in one car
   instead of 100 cars.

**Result:** 20,001 increments/s, **0 errors**, p99 = 13 ms, a new count is visible in 15 ms (p99).
At 40K gRPC holds better than REST: p99 105 ms vs 196 ms.

**What p99 means:** 99% of requests are faster than this number. p99 = 13 ms → only 1 in 100
is slower than 13 ms. More honest than the average: the average hides slow requests.

---

# PART 2. SIS #3: the database

**The idea of SIS #3:** not just "turned on optimizations", but **measured each one before and after**
and explained why it got better (or worse, or no change).

## 8. EXPLAIN ANALYZE, the main tool

- `EXPLAIN` shows the **plan** (GPS: "we'll go this way, ~20 min"). The query does **not** run.
- `EXPLAIN ANALYZE` **runs** the query and shows the **real** time ("drove it, took 35 min").
- We use `EXPLAIN (ANALYZE, BUFFERS)` everywhere: [db/explain.sql](db/explain.sql).
  `BUFFERS` = how many pages were read from disk.
- Queries that change data run inside `BEGIN ... ROLLBACK` (executed, then undone).

How to read it: `Seq Scan` = reads the whole table (bad), `Index Scan` = uses the index (good),
`actual time` = real time, `Execution Time` = total.

## 9. Six changes: what, number, why

### 9.1 Indexes
An index is a book's table of contents. Finding is fast, but **every write also updates the index**.
- Query "who liked post 4242": without an index **433 ms** (78,786 pages read),
  with an index **5.6 ms** (48 pages).
- The cost: inserts are slower (**+63%** p99) and **304 MB** of disk.
- Decision: T7 does not need this query → we did **not** keep the index. Decided by numbers.
- Why slow without it: the key `(user_id, post_id)` is sorted by user_id, like a phone book sorted
  by last name. Searching by post_id = searching by first name: you read the whole book
  (leftmost-prefix rule, Lecture 6).

### 9.2 Partitioning
The `likes` table is split into **16 parts by HASH(user_id)** ([migrations/versions/0002_tuning.py](migrations/versions/0002_tuning.py)).
- Example: 16 boxes instead of one huge one. Query "did user 7 like it?": Postgres knows which box
  user 7 is in and looks in **only 1 of 16** (this is called partition pruning).
- Why not by day: our query searches by user_id → by-day partitions would make it open every box.
  Measured: by day is **2.3× slower**.
- Deleting old data: `DROP` a partition **9 ms**, `DELETE` the same rows **1,131 ms**.
  DROP throws away the whole file; DELETE removes row by row and leaves "garbage" (dead rows).

### 9.3 Worker writes
[app/worker/sync.py](app/worker/sync.py).
- Before: 1000 separate UPDATEs. After: **one** `INSERT ... SELECT FROM unnest(...)`.
  One trip to the database instead of a thousand. p99 **44 → 23 ms**.
- `fillfactor 80`: leave 20% free space on each page. Then an update is written to the same page
  and does not touch the index (**HOT update**). Cheaper.
- `synchronous_commit = off`: do not wait for the disk flush; it is only a copy, the live
  numbers are in Redis.
- Ids are sorted before writing → two concurrent writes take locks in the same order and cannot
  block each other (no deadlock, Lecture 6).

### 9.4 PgBouncer (connection pool)
[db/pgbouncer/](db/pgbouncer/), measured with [db/run_pool.sh](db/run_pool.sh).
- Each Postgres connection is a separate heavy process; the limit is 100.
- PgBouncer is a **doorman**: many clients share **30–40 real connections**. Transaction mode:
  a client gets a connection only for one transaction.
- Measured: direct connections grow **18 → 94** (almost the limit), through PgBouncer a steady
  **30**, same p99.
- Pool **2** per process is too small: **5–7% errors** (queries wait in the queue and time out).
  The best p99 was pool **25**; pool 50 is not better (Lecture 7: "smaller is usually faster").
  The project uses 10: all "after" runs used it. Direct, 25 does not fit
  (8 pools × 25 = 200 > 100 connections); through PgBouncer it does.

### 9.5 Read replica
[db/replica/entrypoint.sh](db/replica/entrypoint.sh), [app/core/resources.py](app/core/resources.py).
- A copy of the database: the primary takes writes, **the replica answers reads**.
- `GetCounters` reads (what is not in Redis, and `liked_by_me`) go to the replica.
- Measured: read p99 **173 → 50 ms**. Replica lag at most **0.6 s** < 2 s → the eventual
  consistency requirement holds.

### 9.6 Postgres config tuning
[db/postgresql.conf](db/postgresql.conf).
- **Honestly: no gain.** The HOT update share even dropped 99% → 65% because of
  `hot_standby_feedback` (the replica asks the primary not to clean up old row versions).
- That is a result too: **measure first, then believe**.

## 10. Numbers for the defense (learn these)

| What | Before → After |
|---|---|
| Index for reads | 433 ms → 5.6 ms (but writes +63%) |
| DELETE vs DROP partition | 1,131 ms vs 9 ms |
| By-day vs HASH partitions | by day 2.3× slower |
| unnest in the worker | p99 44 → 23 ms |
| PgBouncer | connections 94 → 30, same p99 |
| Replica | read p99 173 → 50 ms, lag ≤ 0.6 s |
| Config tuning | no gain |

---

## 11. Where things are in the code

```
app/
  core/        ← all logic (like, view, read, Lua scripts in cache.py)
  rest/        ← the REST door (FastAPI)
  grpc_api/    ← the gRPC door
  worker/      ← sync-worker: Redis → Postgres
proto/         ← gRPC contract (counter.proto)
clients/       ← clients with timeouts and retries
migrations/    ← DB schema (0001 = before, 0002 = after SIS #3)
db/            ← Postgres, PgBouncer, replica configs, SIS #3 measurement scripts
benchmark/     ← measurement scripts + results (benchmark/db/*.json, explain_*.txt)
loadtest/      ← k6 load scenarios (20K)
tests/         ← 59 tests
```

## 12. Explain it to the instructor in 1 minute

> "T7 counts likes and views at 20K per second. Every +1 goes to Redis, so it is fast, and a worker
> saves them to Postgres in batches every 5 seconds. Inside the system we use gRPC: it is smaller
> and holds the load better than REST. All operations are idempotent, so retries are safe.
>
> In SIS #3 I optimized Postgres and measured every change with EXPLAIN ANALYZE and load tests:
> HASH partitions read 1 of 16 parts, one unnest instead of a thousand UPDATEs is 2× faster,
> PgBouncer keeps 30 connections instead of 94, and the replica made reads 3.5× faster. Where there
> was no gain, like config tuning, I showed that too."
