"""Fixed-rate benchmark: REST and gRPC get the SAME number of requests per second.

run_benchmark.py finds the limit of each protocol (same concurrency, different RPS).
This script compares latency at the same load. Run it on a clean database:

    docker compose down -v
    docker compose up -d --build
    docker compose run --rm benchmark python -m benchmark.run_fixed_rate
"""

import argparse
import asyncio
import csv
import statistics
import time
from pathlib import Path

from benchmark.run_benchmark import OPERATIONS, USER_ID_BASE, WARMUP_USER_OFFSET, make_call, run_load
from clients.grpc_client import GrpcCounterClient
from clients.rest_client import RestCounterClient


async def run_at_fixed_rate(call, rate: int, duration: int) -> tuple[list[float], int, float]:
    """Send `rate` requests per second. Returns (latencies in ms, errors, achieved RPS).

    Latency starts at the PLANNED send time. If the system falls behind, the waiting is counted.
    """
    total = rate * duration
    latencies_ms: list[float] = []
    errors = 0

    async def send_one(number: int, planned_time: float) -> None:
        nonlocal errors
        try:
            await call(number)
        except Exception:
            errors += 1
        else:
            latencies_ms.append((time.perf_counter() - planned_time) * 1000)

    started = time.perf_counter()
    tasks = []
    for number in range(total):
        planned_time = started + number / rate
        await asyncio.sleep(max(0, planned_time - time.perf_counter()))
        tasks.append(asyncio.create_task(send_one(number, planned_time)))
    await asyncio.gather(*tasks)
    return latencies_ms, errors, len(latencies_ms) / (time.perf_counter() - started)


def summarize(protocol: str, operation: str, rate: int, result: tuple) -> dict:
    latencies, errors, achieved_rps = result
    percentiles = statistics.quantiles(latencies, n=100)
    return {
        "protocol": protocol,
        "operation": operation,
        "target_rps": rate,
        "achieved_rps": round(achieved_rps, 1),
        "errors": errors,
        "mean_ms": round(statistics.fmean(latencies), 2),
        "p50_ms": round(percentiles[49], 2),
        "p95_ms": round(percentiles[94], 2),
        "p99_ms": round(percentiles[98], 2),
    }


def save_results(rows: list[dict], out_dir: Path) -> None:
    with (out_dir / "fixed_rate_results.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# Fixed-rate results: same RPS for REST and gRPC",
        "",
        "| Protocol | Operation | target RPS | achieved RPS | errors | p50 ms | p95 ms | p99 ms | mean ms |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['protocol']} | {r['operation']} | {r['target_rps']} | {r['achieved_rps']} | "
            f"{r['errors']} | {r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} | {r['mean_ms']} |"
        )
    (out_dir / "fixed_rate_results.md").write_text("\n".join(lines) + "\n")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rest-url", default="http://rest-api:8000")
    parser.add_argument("--grpc-target", default="grpc-api:50051")
    parser.add_argument("--rate", type=int, default=300)
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()

    rows = []
    async with (
        RestCounterClient(args.rest_url) as rest_client,
        GrpcCounterClient(args.grpc_target) as grpc_client,
    ):
        for operation in OPERATIONS:
            for protocol, client in (("REST", rest_client), ("gRPC", grpc_client)):
                call = make_call(client, operation, USER_ID_BASE[protocol])
                await run_load(call, WARMUP_USER_OFFSET, 500, 50)
                result = await run_at_fixed_rate(call, args.rate, args.duration)
                row = summarize(protocol, operation, args.rate, result)
                rows.append(row)
                print(
                    f"{protocol:4} {operation:17} rps={row['achieved_rps']:6.1f}  p50={row['p50_ms']:7.2f}  "
                    f"p95={row['p95_ms']:7.2f}  p99={row['p99_ms']:7.2f}  errors={row['errors']}"
                )
    save_results(rows, args.out_dir)


if __name__ == "__main__":
    asyncio.run(main())
