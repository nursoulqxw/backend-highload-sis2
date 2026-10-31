"""Benchmark: REST/JSON vs gRPC, same server logic, same data, same operations.

Start everything first:  docker compose up -d --build
Then run:                python -m benchmark.run_benchmark

For each operation: 500 warm-up requests, then 10,000 measured requests with 50
running at the same time. We measure latency (p50, p95, p99, mean), throughput
(requests per second) and the body size of one request and one response.
"""

import argparse
import asyncio
import csv
import json
import platform
import random
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import grpc
import httpx

from app.grpc_api.generated import counter_pb2, counter_pb2_grpc
from benchmark.charts import draw_latency_chart, draw_payload_chart
from clients.grpc_client import GrpcCounterClient
from clients.rest_client import RestCounterClient

SLO_P99_MS = 100  # from SIS #1: p99 < 100 ms

# Realistic ids: 50M posts, so post ids go up to 50,000,000. 1000 "popular" posts.
RANDOM_SEED = 42
HOT_POSTS = random.Random(RANDOM_SEED).sample(range(1, 50_000_001), 1000)

# Each protocol gets its own users, so every like / view is new and does the real work
# (INSERT + HINCRBY), not the cheap "already liked" path. Both ranges have the same
# number of digits, so the payload sizes are the same.
USER_ID_BASE = {"REST": 1_000_000_000, "gRPC": 2_000_000_000}
WARMUP_USER_OFFSET = 500_000_000

OPERATIONS = ["LikePost", "RecordView", "GetCounters x1", "GetCounters x10", "GetCounters x100"]


@dataclass
class Result:
    protocol: str
    operation: str
    requests: int
    errors: int
    concurrency: int
    throughput_rps: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    request_bytes: int
    response_bytes: int


def post_ids_for(operation: str, request_number: int) -> list[int]:
    """GetCounters xN -> N post ids. The same request number gives the same ids for both protocols."""
    count = int(operation.split("x")[1])
    return random.Random(request_number).sample(HOT_POSTS, count)


def make_call(client, operation: str, user_base: int) -> Callable[[int], Awaitable]:
    """Return a function that sends request number i of this operation."""
    if operation == "LikePost":
        return lambda i: client.like_post(user_base + i, HOT_POSTS[i % len(HOT_POSTS)])
    if operation == "RecordView":
        return lambda i: client.record_view(user_base + i, HOT_POSTS[i % len(HOT_POSTS)])
    return lambda i: client.get_counters(user_base + i, post_ids_for(operation, i))


async def run_load(call: Callable[[int], Awaitable], first: int, count: int, concurrency: int):
    """Send `count` requests, `concurrency` at a time. Returns (latencies in ms, errors, seconds)."""
    request_numbers = iter(range(first, first + count))
    latencies_ms: list[float] = []
    errors = 0

    async def worker() -> None:
        nonlocal errors
        for i in request_numbers:  # all workers share one iterator
            started = time.perf_counter()
            try:
                await call(i)
            except Exception:
                errors += 1
            else:
                latencies_ms.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return latencies_ms, errors, time.perf_counter() - started


async def measure_rest_payload(base_url: str, operation: str) -> tuple[int, int]:
    """Sizes of one request and response as JSON.

    REST sends the request data in the URL and a header, not in a body. To compare the same
    data, we measure the same fields as the Protobuf request, written as JSON.
    """
    user_id = USER_ID_BASE["REST"] + 999_999_999
    headers = {"X-User-Id": str(user_id)}
    async with httpx.AsyncClient(base_url=base_url) as http:
        if operation == "LikePost":
            request_data = {"user_id": user_id, "post_id": HOT_POSTS[0]}
            response = await http.put(f"/api/v1/posts/{HOT_POSTS[0]}/like", headers=headers)
        elif operation == "RecordView":
            request_data = {"user_id": user_id, "post_id": HOT_POSTS[0]}
            response = await http.post(f"/api/v1/posts/{HOT_POSTS[0]}/view", headers=headers)
        else:
            post_ids = post_ids_for(operation, 0)
            request_data = {"user_id": user_id, "post_ids": post_ids}
            params = {"post_ids": ",".join(map(str, post_ids))}
            response = await http.get("/api/v1/counters", params=params, headers=headers)
    response.raise_for_status()
    request_json = json.dumps(request_data, separators=(",", ":")).encode()
    return len(request_json), len(response.content)


async def measure_grpc_payload(target: str, operation: str) -> tuple[int, int]:
    """Sizes of one request and response as Protobuf."""
    user_id = USER_ID_BASE["gRPC"] + 999_999_999
    async with grpc.aio.insecure_channel(target) as channel:
        stub = counter_pb2_grpc.CounterServiceStub(channel)
        if operation == "LikePost":
            request = counter_pb2.LikeRequest(user_id=user_id, post_id=HOT_POSTS[0])
            response = await stub.LikePost(request)
        elif operation == "RecordView":
            request = counter_pb2.ViewRequest(user_id=user_id, post_id=HOT_POSTS[0])
            response = await stub.RecordView(request)
        else:
            request = counter_pb2.GetCountersRequest(user_id=user_id, post_ids=post_ids_for(operation, 0))
            response = await stub.GetCounters(request)
    return len(request.SerializeToString()), len(response.SerializeToString())


async def benchmark_operation(
    protocol: str, client, operation: str, args, payload: tuple[int, int]
) -> Result:
    call = make_call(client, operation, USER_ID_BASE[protocol])
    await run_load(call, WARMUP_USER_OFFSET, args.warmup, args.concurrency)
    latencies, errors, seconds = await run_load(call, 0, args.requests, args.concurrency)

    percentiles = statistics.quantiles(latencies, n=100)  # 99 cut points: [49] = p50, [98] = p99
    return Result(
        protocol=protocol,
        operation=operation,
        requests=args.requests,
        errors=errors,
        concurrency=args.concurrency,
        throughput_rps=round(len(latencies) / seconds, 1),
        mean_ms=round(statistics.fmean(latencies), 2),
        p50_ms=round(percentiles[49], 2),
        p95_ms=round(percentiles[94], 2),
        p99_ms=round(percentiles[98], 2),
        request_bytes=payload[0],
        response_bytes=payload[1],
    )


def save_csv(results: list[Result], path: Path) -> None:
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(asdict(results[0])))
        writer.writeheader()
        writer.writerows(asdict(r) for r in results)


def meets_slo(p99_ms: float) -> str:
    return "yes" if p99_ms < SLO_P99_MS else "NO"


def ratio(a: float, b: float) -> str:
    return f"{a / b:.1f}x" if b else "-"


def save_markdown(results: list[Result], args, path: Path) -> None:
    by_key = {(r.protocol, r.operation): r for r in results}
    lines = [
        "# Benchmark results: REST/JSON vs gRPC",
        "",
        f"- Date: {datetime.now():%Y-%m-%d %H:%M}",
        f"- Machine: {platform.platform()}, {platform.machine()}, Python {platform.python_version()}",
        f"- Load: {args.warmup} warm-up + {args.requests} measured requests per operation, "
        f"concurrency {args.concurrency}",
        "- Servers: `docker compose` (rest-api: 1 Uvicorn process, grpc-api: 1 process), "
        "same Postgres + Redis, same `app/core` code",
        "- Clients: `clients/rest_client.py` (httpx, HTTP/1.1 keep-alive) and "
        "`clients/grpc_client.py` (grpcio, HTTP/2), both with deadline 0.5 s + retries",
        "",
        "## All numbers",
        "",
        "| Protocol | Operation | p50 ms | p95 ms | p99 ms | mean ms | RPS | errors "
        "| request B | response B |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r.protocol} | {r.operation} | {r.p50_ms} | {r.p95_ms} | {r.p99_ms} | {r.mean_ms} | "
            f"{r.throughput_rps} | {r.errors} | {r.request_bytes} | {r.response_bytes} |"
        )

    lines += [
        "",
        "## gRPC compared to REST",
        "",
        "How many times gRPC is better (bigger = better for gRPC; below 1.0x = REST is better).",
        "",
        "| Operation | p50 faster | p99 faster | more RPS | request smaller | response smaller | "
        f"REST p99 < {SLO_P99_MS} ms | gRPC p99 < {SLO_P99_MS} ms |",
        "|---|---:|---:|---:|---:|---:|:---:|:---:|",
    ]
    for operation in OPERATIONS:
        rest_result, grpc_result = by_key[("REST", operation)], by_key[("gRPC", operation)]
        lines.append(
            f"| {operation} | {ratio(rest_result.p50_ms, grpc_result.p50_ms)} | "
            f"{ratio(rest_result.p99_ms, grpc_result.p99_ms)} | "
            f"{ratio(grpc_result.throughput_rps, rest_result.throughput_rps)} | "
            f"{ratio(rest_result.request_bytes, grpc_result.request_bytes)} | "
            f"{ratio(rest_result.response_bytes, grpc_result.response_bytes)} | "
            f"{meets_slo(rest_result.p99_ms)} | {meets_slo(grpc_result.p99_ms)} |"
        )

    lines += ["", "## Conclusion", "", *conclusion_lines(results)]
    lines += [
        "",
        "## Notes",
        "",
        "- Payload = the same data as JSON bytes vs Protobuf `SerializeToString()` bytes. The REST request "
        "data really travels in the URL and the `X-User-Id` header, so `request B` for REST is the size of "
        "the same fields written as JSON. HTTP and gRPC headers are not counted. LikePost and RecordView "
        "answer 204 / Empty, so their response is 0 bytes in both protocols.",
        "- The biggest size difference is in GetCounters: JSON repeats the field names "
        '(`"post_id"`, `"likes"`, ...) for every post; Protobuf sends small field numbers '
        "and compact numbers (varints).",
        "- GetCounters x100: here the server work (100 Redis reads + 1 SQL query) is most of the "
        "time, so the two protocols are close, and its p99 changes a lot from run to run.",
        "- errors = calls that did not finish inside the client deadline (0.5 s, all retries included). "
        "They are counted, but not included in the latency numbers.",
        "- The benchmark runs as a container in the same Docker network as the servers. On a Mac, "
        "going through Docker Desktop port forwarding made REST (50 TCP connections) 3-4x slower, "
        "but not gRPC (1 HTTP/2 connection), so that would not be a fair test.",
        "- Latency includes the client library. httpx is written in Python; the gRPC client "
        "is written in C. At concurrency 50 the httpx client uses a full CPU core, which is a big part "
        "of the REST tail latency (p99). This is part of the real cost of each protocol in Python.",
        "",
        "![Latency](latency_p50_p99.png)",
        "",
        "![Payload size](payload_size.png)",
        "",
    ]
    path.write_text("\n".join(lines))


def ratio_range(ratios: list[float]) -> str:
    return f"{min(ratios):.1f}-{max(ratios):.1f}x"


def conclusion_lines(results: list[Result]) -> list[str]:
    """The conclusion, computed from the results. Used in results.md and in the terminal."""
    by_key = {(r.protocol, r.operation): r for r in results}
    pairs = {op: (by_key[("REST", op)], by_key[("gRPC", op)]) for op in OPERATIONS}
    small_calls = [pairs[op] for op in OPERATIONS if op != "GetCounters x100"]
    big_rest, big_grpc = pairs["GetCounters x100"]
    slo_rest = sum(r.p99_ms < SLO_P99_MS for r, _ in pairs.values())
    slo_grpc = sum(g.p99_ms < SLO_P99_MS for _, g in pairs.values())
    request_ratios = [r.request_bytes / g.request_bytes for r, g in pairs.values()]
    response_ratios = [r.response_bytes / g.response_bytes for r, g in pairs.values() if g.response_bytes]
    return [
        f"- **Smaller:** Protobuf requests are {ratio_range(request_ratios)} smaller than the same data "
        f"as JSON, and GetCounters responses are {ratio_range(response_ratios)} smaller.",
        f"- **Faster for small calls** (like, view, GetCounters x1 and x10): gRPC p50 is "
        f"{ratio_range([r.p50_ms / g.p50_ms for r, g in small_calls])} lower, p99 is "
        f"{ratio_range([r.p99_ms / g.p99_ms for r, g in small_calls])} lower, throughput is "
        f"{ratio_range([g.throughput_rps / r.throughput_rps for r, g in small_calls])} higher.",
        f"- **GetCounters x100:** p50 {ratio(big_rest.p50_ms, big_grpc.p50_ms)}, "
        f"p99 {ratio(big_rest.p99_ms, big_grpc.p99_ms)}, "
        f"throughput {ratio(big_grpc.throughput_rps, big_rest.throughput_rps)}. "
        "The speed gain is much smaller here, because the server work takes most of the time.",
        f"- **SLO p99 < {SLO_P99_MS} ms:** REST meets it in {slo_rest} of {len(pairs)} operations, "
        f"gRPC in {slo_grpc} of {len(pairs)}, on one process and this load.",
        "- **Why smaller:** JSON repeats field names and writes numbers as text. Protobuf sends field "
        "numbers and compact numbers.",
        "- **Why faster:** the server logic is the same code. The difference comes from the rest: "
        "JSON vs Protobuf work, HTTP/1.1 with 50 connections vs HTTP/2 with one, and a Python vs C client.",
        "- **When REST is still fine:** public APIs for browsers and mobile apps, easy debugging with "
        "curl, and small or rare calls, where a few bytes or a millisecond do not matter.",
    ]


def print_conclusion(results: list[Result]) -> None:
    print("\n=== Conclusion ===")
    for line in conclusion_lines(results):
        print(line.replace("**", ""))


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rest-url", default="http://localhost:8000")
    parser.add_argument("--grpc-target", default="localhost:50051")
    parser.add_argument("--requests", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()

    results: list[Result] = []
    async with (
        RestCounterClient(args.rest_url, max_connections=args.concurrency) as rest_client,
        GrpcCounterClient(args.grpc_target) as grpc_client,
    ):
        for operation in OPERATIONS:
            for protocol, client in (("REST", rest_client), ("gRPC", grpc_client)):
                if protocol == "REST":
                    payload = await measure_rest_payload(args.rest_url, operation)
                else:
                    payload = await measure_grpc_payload(args.grpc_target, operation)
                result = await benchmark_operation(protocol, client, operation, args, payload)
                results.append(result)
                print(
                    f"{protocol:4} {operation:17} p50={result.p50_ms:6.2f} ms  p99={result.p99_ms:6.2f} ms  "
                    f"rps={result.throughput_rps:8.1f}  errors={result.errors}"
                )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    save_csv(results, args.out_dir / "results.csv")
    save_markdown(results, args, args.out_dir / "results.md")
    rows = [asdict(r) for r in results]
    draw_latency_chart(rows, OPERATIONS, args.out_dir / "latency_p50_p99.png", SLO_P99_MS)
    draw_payload_chart(rows, OPERATIONS, args.out_dir / "payload_size.png")
    print(f"\nSaved results.csv, results.md and 2 charts to {args.out_dir}/")
    print_conclusion(results)


if __name__ == "__main__":
    asyncio.run(main())
