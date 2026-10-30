# Defense notes — SIS #2, T7 Like & View Counter

## 3-minute speech

> **1. The task (20 s).**
> My system is T7, a like and view counter. It must handle 20 thousand requests per second
> and 50 million posts. In SIS #1 I chose gRPC for internal calls. In SIS #2 I implemented it,
> added idempotency, timeouts and retries, and measured it against a REST/JSON baseline.
>
> **2. The design (40 s).**
> There is one core module, `app/core`, with all the logic. Two thin layers sit on top of it:
> REST with FastAPI, and gRPC with grpcio. Both call the same functions. So in the benchmark
> the only difference is the protocol.
> Live counters are in Redis, because it is in memory and very fast. Likes are in Postgres,
> because the primary key `(user_id, post_id)` gives us dedupe for free. A sync worker copies
> the counters from Redis to Postgres every 5 seconds.
>
> **3. Idempotency, timeouts, retries (50 s).**
> All write operations are idempotent. A like is `INSERT ... ON CONFLICT DO NOTHING`, and the
> counter changes only if a row was really inserted. A view uses Redis `SET NX` with a 1-hour TTL.
> So if a client sends the same call twice, the result is the same.
> This is why retries are safe. The client retries at most 3 times, only on "try again later"
> errors (UNAVAILABLE or 503), with exponential backoff and jitter, and never after the deadline.
> The client deadline is 0.5 s. The server gives Postgres and Redis 200 ms each. If they are
> slower, the server answers 503 or UNAVAILABLE at once. Nothing hangs.
> I tested this with fault injection: the server can fail on purpose, or be slow on purpose.
> There are 43 tests, and the reliability tests run for both protocols.
>
> **4. Results (50 s).**
> 10 thousand requests per operation, 50 at a time, the same machine and the same data.
> - Protobuf responses are about 5.7 times smaller than JSON, because JSON repeats the field names.
> - For likes, views and small reads, gRPC is about 3 times faster at p50 and more than 10 times
>   faster at p99, with about 4 times more throughput.
> - gRPC meets my SLO from SIS #1, p99 under 100 ms. REST does not, at this load.
> - With 100 posts per request, the server work is most of the time, so the speed is about the
>   same. But the payload is still 5.7 times smaller.
>
> **5. Conclusion (20 s).**
> gRPC is the right choice inside the system, between the gateway and the services.
> REST stays outside, for browsers and mobile apps, because everyone understands it and it is
> easy to debug.

## How to show it live (if asked)

```bash
docker compose up -d --build                      # 5 containers
curl -X PUT -H "X-User-Id: 1" localhost:8000/api/v1/posts/42/like
curl -X PUT -H "X-User-Id: 1" localhost:8000/api/v1/posts/42/like   # again: still 1 like
curl -H "X-User-Id: 1" "localhost:8000/api/v1/counters?post_ids=42"
pytest tests/test_reliability.py -v               # idempotency, timeouts, retries, both protocols
make lint                                         # code style
```

Then open `benchmark/results.md` and the two charts.

## Map: where is each thing in the code?

| Topic | File | Function / place |
|---|---|---|
| Like / unlike logic | `app/core/likes.py` | `like_post`, `unlike_post` |
| View logic | `app/core/views.py` | `record_view` |
| Get counters (cache-aside) | `app/core/counters.py` | `get_counters` |
| Cold key rule | `app/core/cache.py` | `ensure_loaded` |
| gRPC contract | `proto/counter.proto` | `service CounterService` |
| gRPC server | `app/grpc_api/server.py` | `CounterService` class |
| REST server | `app/rest/routes.py`, `app/rest/main.py` | the 4 routes, 503 handler |
| Server timeouts (200 ms) | `app/core/resources.py` | `command_timeout`, `socket_timeout` |
| 503 / UNAVAILABLE mapping | `app/core/errors.py` + `app/rest/main.py` + `app/grpc_api/server.py` | `BACKEND_ERRORS`, `backend_call` |
| gRPC retries + deadline | `clients/grpc_client.py` | `SERVICE_CONFIG`, `timeout=0.5` |
| REST retries + deadline | `clients/rest_client.py` | `send`, `backoff_seconds` |
| Fault injection | `app/core/faults.py` | `FaultInjector.around_call` |
| Benchmark | `benchmark/run_benchmark.py` | `run_load`, `benchmark_operation` |

## 15 likely questions with short answers

**1. Why gRPC and not REST?**
Inside the system we need speed and small messages. gRPC uses binary Protobuf, so messages are
about 5.7x smaller. It uses HTTP/2, so many calls go over one connection at the same time.
It has a strict contract (`.proto`), and deadlines and retries are built in.
My benchmark shows about 3x faster p50 and more than 10x faster p99 for small calls.

**2. What is Protobuf?**
A binary format for data from Google. You describe messages in a `.proto` file. Each field has
a number (`user_id = 1`). On the wire, Protobuf sends the field number and the value, not the
field name. Numbers use "varints", so small numbers take fewer bytes. Code to read and write
the messages is generated from the `.proto` file.

**3. Why is JSON bigger?**
JSON is text. It repeats every field name for every object: `"post_id"`, `"likes"`, `"views"`,
`"liked_by_me"`. Numbers are written as text, digit by digit. For 100 posts that was 6,281 bytes
in JSON and 1,098 bytes in Protobuf.

**4. HTTP/2 vs HTTP/1.1?**
HTTP/1.1 sends one request at a time on a connection. To send 50 requests at once you need 50
connections. HTTP/2 has **multiplexing**: many requests (streams) at the same time on one
connection. It also compresses headers (HPACK) and is binary. Our gRPC client used 1 connection;
the REST client needed 50.

**5. What is idempotency?**
Doing the same call twice gives the same result as doing it once. Our like: the second
`INSERT ... ON CONFLICT DO NOTHING` inserts 0 rows, so the counter does not change. Unlike: the
second DELETE deletes 0 rows. View: the second `SET NX` in the same hour does nothing.

**6. Why `PUT` for like and not `POST` or `PATCH`?**
A like is a state: liked or not liked. `PUT` means "set this state", and HTTP says `PUT` is
idempotent. `POST` and `PATCH` are not idempotent by the standard: `PATCH {"likes": "+1"}`
repeated twice gives +2. A view is an event, so it is `POST`, but we make it idempotent for
1 hour with `SET NX`.

**7. Why retry only idempotent calls?**
When a call fails, the client often does not know if the server did the work. Maybe only the
answer was lost. If the call is not idempotent, a retry can do the work twice, for example count
2 likes. If it is idempotent, the retry is safe. I have a test for this: the server does the
work, then "loses" the answer, the client retries, and the count is still 1.

**8. Timeout vs deadline?**
A **timeout** is a duration: "wait at most 0.5 s". A **deadline** is a point in time:
"the answer must come before 12:00:00.500". gRPC sends the deadline to the server, so the server
knows how much time is left. In our REST client we turn the timeout into a deadline. All retries
must fit before it, and each attempt only gets the time that is left.

**9. Why is the server timeout (200 ms) shorter than the client deadline (500 ms)?**
If Postgres or Redis hangs, the server gives up after 200 ms and answers 503 / UNAVAILABLE.
The client still has about 300 ms left for a retry. If the server waited longer than the
client, the client would give up first, and the server would keep working for nobody.

**10. What is exponential backoff with jitter?**
Backoff: wait before a retry, and wait longer each time (50 ms, 100 ms, max 200 ms), so we
do not overload a server that is already in trouble. Jitter: the wait is random, from 0 up to
that value. Without jitter, 1000 clients that failed at the same moment would all retry at the
same moment again (a "retry storm" or "thundering herd").

**11. Which errors do you retry?**
Only "try again later": gRPC `UNAVAILABLE`, REST `502/503/504` or a lost connection. Not `422`
or `INVALID_ARGUMENT`, because the request is wrong and a retry will not fix it. Not
`DEADLINE_EXCEEDED`, because then there is no time left.

**12. What do p50 and p99 mean?**
Sort all request times. **p50** (the median) is the middle one: half of the requests were
faster. It is the "typical" user. **p99**: 99% were faster, 1% were slower. It shows the slow
"tail". At 20k RPS, 1% is 200 requests every second, so p99 matters a lot. That is why my SLO
is on p99.

**13. What do the benchmark numbers mean? Is it fair?**
Same machine, same servers (1 process each), same database and the same `app/core` code,
10,000 requests, concurrency 50, 500 warm-up requests. So the difference is the protocol and its
client library. Honest limits: the Python REST client (httpx) uses a full CPU core at this load,
and the gRPC client is written in C. That is part of the result. The benchmark runs inside
Docker, because Docker Desktop port forwarding on a Mac slowed down REST 3–4x and gRPC not at
all, which would be unfair.

**14. Why is GetCounters with 100 posts almost the same speed?**
There the server does more work: 100 Redis reads and a SQL query. That work takes most of the
time, so the protocol is a small part of the total. The payload is still 5.7x smaller, which
saves network bandwidth.

**15. When is REST still a good choice?**
Public APIs for browsers and mobile apps: every client understands HTTP + JSON, and browsers
cannot call gRPC directly. REST is easy to debug with `curl` and to read in logs. For small or
rare calls, a few bytes or milliseconds do not matter. So: REST outside, gRPC inside.

## Extra questions that may come up

**What happens if Redis loses the data?**
Postgres has a copy in `post_counters`, at most about 5 s old. The "cold key" rule loads it
back into Redis with `HSETNX` before the next increment, so a counter never restarts from 0.

**What is fault injection, and is it in production?**
Test-only settings (`FAULT_FAIL_FIRST_N`, `FAULT_FAIL_AFTER_WORK`, `FAULT_DELAY_MS`) that make
the server fail or be slow on purpose. They are all off (0) by default.

**What are the strange names in `counter_pb2_grpc.py`?**
That file is generated by `protoc` from `proto/counter.proto` (`make proto`). We never edit it.
Its names come from the tool. Our own code is checked by `ruff` (`make lint`), and the generated
folder is excluded.

**Why does REST have errors in the benchmark?**
Those calls did not finish inside the 0.5 s client deadline. Under load, the Python REST client
is busy and its tail latency grows. They are counted in the `errors` column, not hidden.
