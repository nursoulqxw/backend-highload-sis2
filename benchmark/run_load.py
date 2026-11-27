"""Load test matrix with k6: can T7 take 20K increments/s? Runs on the host (needs Docker).

    python -m benchmark.run_load            # full matrix, ~25 min
    python -m benchmark.run_load --quick    # short runs, ~8 min, to check that it works
    python -m benchmark.run_load --report   # only redraw report + chart from saved JSON

What it runs (all at a FIXED rate, see loadtest/increments.js):
  1. unary: one view / like per call, rising rate -> the ceiling of each protocol.
     With 1 and 4 server processes -> how the ceiling grows with CPU cores.
  2. batch: views in batches of 100 (as the API Gateway sends them), likes unary.
     At 20K increments/s (the requirement) and 40K (headroom).

Before each run: FLUSHALL Redis + a 10 s warm-up, so runs do not affect each other
(old seen:* keys would fill Redis memory) and every run starts from the same state.

Output: benchmark/load/*.json (raw k6 summaries), benchmark/load_results.md, benchmark/load_chart.png
"""

import argparse
import json
import os
import platform
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "benchmark" / "load"
K6_IMAGE = "grafana/k6:1.0.0"
NETWORK = "backend-highload-sis2_default"
SLO_P99_MS = 100  # from SIS #1
TARGET = 20_000  # increments/s, from SIS #1

PROTOCOLS = ["rest", "grpc"]
UNARY_RATES = {1: [1000, 2000, 3000, 4000], 4: [2000, 4000, 6000, 8000, 10000, 12000]}
BATCH_RATES = [20_000, 40_000]


def sh(cmd: list[str], env: dict | None = None, check: bool = True) -> str:
    result = subprocess.run(cmd, cwd=ROOT, env={**os.environ, **(env or {})}, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def start_servers(workers: int) -> None:
    """(Re)start REST and gRPC with N processes each and wait until they are healthy."""
    print(f"== servers with {workers} process(es) each")
    sh(
        ["docker", "compose", "up", "-d", "--wait", "rest-api", "grpc-api", "sync-worker"],
        {"WORKERS": str(workers)},
    )


def flush_redis() -> None:
    sh(["docker", "compose", "exec", "-T", "redis", "redis-cli", "FLUSHALL"])


def k6(name: str, env: dict) -> dict | None:
    args = ["docker", "run", "--rm", "--network", NETWORK]
    args += ["-v", f"{ROOT}/loadtest:/scripts", "-v", f"{ROOT}/proto:/proto", "-v", f"{OUT_DIR}:/results"]
    for key, value in {**env, "OUT": f"/results/{name}.json"}.items():
        args += ["-e", f"{key}={value}"]
    args += [K6_IMAGE, "run", "-q", "--no-color", "/scripts/increments.js"]
    sh(args, check=False)  # k6 exits non-zero on dropped iterations etc.; we read the summary file
    path = OUT_DIR / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


_user_base = 1_000_000_000


def next_user_base() -> int:
    """Every run (and warm-up) gets new user ids, so every like / view is new."""
    global _user_base
    _user_base += 10_000_000
    return _user_base


def done(name: str) -> bool:
    """A finished run has its JSON with the "workers" field (added after k6 ends)."""
    path = OUT_DIR / f"{name}.json"
    return path.exists() and "workers" in json.loads(path.read_text())


def measure(name: str, env: dict, duration: int, warmup: int) -> None:
    if done(name):  # resume: a run that was stopped half-way skips the finished points
        print(f"   {name}: already done, skipped")
        return
    flush_redis()
    k6("_warmup", {**env, "DURATION": f"{warmup}s", "PROBE": "0", "USER_BASE": next_user_base()})
    result = k6(name, {**env, "DURATION": f"{duration}s", "USER_BASE": next_user_base()})
    if result:
        result["workers"] = env["WORKERS_INFO"]
        (OUT_DIR / f"{name}.json").write_text(json.dumps(result, indent=2))
        p99 = result["view_call_ms"]["p99"] if result["view_call_ms"] else None
        print(
            f"   {name}: {result['achieved_increments_per_s']:.0f}/{result['target_increments_per_s']} inc/s,"
            f" p99 {p99:.1f} ms, failed {result['failed_calls']}, dropped {result['dropped_calls']}"
        )


def run_matrix(quick: bool) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    duration, warmup = (15, 5) if quick else (30, 10)

    for workers in (1, 4):
        names = [f"unary_{p}_w{workers}_{r}" for p in PROTOCOLS for r in UNARY_RATES[workers]]
        if all(done(n) for n in names):
            continue
        start_servers(workers)
        for protocol in PROTOCOLS:
            for rate in UNARY_RATES[workers]:
                env = {"PROTOCOL": protocol, "MODE": "unary", "RATE": rate, "WORKERS_INFO": workers}
                measure(f"unary_{protocol}_w{workers}_{rate}", env, duration, warmup)

    start_servers(4)  # no-op if already running with 4
    for protocol in PROTOCOLS:
        for rate in BATCH_RATES:
            env = {"PROTOCOL": protocol, "MODE": "batch", "BATCH": 100, "RATE": rate, "WORKERS_INFO": 4}
            measure(f"batch_{protocol}_w4_{rate}", env, duration * 2 if rate == TARGET else duration, warmup)


# ---------- report ----------


def load_results() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(OUT_DIR.glob("*.json")) if not p.name.startswith("_")]


def sustained(r: dict) -> bool:
    """The run kept up: >= 99% of the target rate, < 0.1% failed calls, p99 inside the SLO."""
    calls = r["calls_per_s"] * int(r["duration"].rstrip("s"))
    p99 = max(x["p99"] for x in (r["view_call_ms"], r["like_call_ms"]) if x)
    return (
        r["achieved_increments_per_s"] >= 0.99 * r["target_increments_per_s"]
        and r["failed_calls"] <= 0.001 * calls
        and p99 < SLO_P99_MS
    )


def fmt(x: float | None, digits: int = 1) -> str:
    return "-" if x is None else f"{x:,.{digits}f}"


def payload_sizes() -> dict:
    """Bytes of a batch of 100 views: JSON vs Protobuf (the same ids)."""
    from app.grpc_api.generated import counter_pb2

    views = [(1_000_000_000 + i, (i * 2654435761) % 50_000_000 + 1) for i in range(100)]
    as_json = json.dumps({"views": [{"user_id": u, "post_id": p} for u, p in views]}, separators=(",", ":"))
    as_proto = counter_pb2.RecordViewsRequest(
        views=[counter_pb2.ViewRequest(user_id=u, post_id=p) for u, p in views]
    ).SerializeToString()
    return {"json": len(as_json.encode()), "protobuf": len(as_proto)}


def write_report() -> None:
    results = load_results()
    unary = [r for r in results if r["mode"] == "unary"]
    batch = [r for r in results if r["mode"] == "batch"]
    sizes = payload_sizes()
    name = {"rest": "REST", "grpc": "gRPC"}

    lines = [
        "# Load test: 20K increments/s (k6, fixed rate)",
        "",
        f"- Date: {datetime.now():%Y-%m-%d %H:%M}",
        f"- Machine: {platform.machine()}, Docker VM with 8 CPUs / 4 GB RAM. Everything on one laptop:"
        " k6, both servers, Redis, Postgres.",
        "- Tool: k6 `constant-arrival-rate` (open model): requests arrive at a fixed rate even when the"
        " server is slow, so queueing shows up in the latency.",
        "- Workload: 95% views, 5% likes, every (user, post) pair new. 90% of the increments go to 100K hot"
        " posts, 10% to any of 50M posts (cold keys -> Postgres reads).",
        f"- Kept up = at least 99% of the target rate, under 0.1% failed calls, p99 < {SLO_P99_MS} ms (SLO).",
        "",
        "## 1. Batched views: the requirement (20K increments/s)",
        "",
        "Views go in batches of 100 (`RecordViews`), likes one by one (`LikePost`).",
        "",
        "| Protocol | target inc/s | achieved inc/s | calls/s | failed | batch p50 ms | batch p99 ms"
        " | like p99 ms | consistency lag p50 / p99 ms | kept up |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for r in sorted(batch, key=lambda r: (r["target_increments_per_s"], r["protocol"])):
        v, lk, lag = r["view_call_ms"], r["like_call_ms"], r["consistency_lag_ms"]
        lines.append(
            f"| {name[r['protocol']]} | {r['target_increments_per_s']:,}"
            f" | {fmt(r['achieved_increments_per_s'], 0)} | {r['calls_per_s']:,} | {r['failed_calls']}"
            f" | {fmt(v['p50'])} | {fmt(v['p99'])}"
            f" | {fmt(lk['p99'] if lk else None)} | {fmt(lag['p50'] if lag else None, 0)} /"
            f" {fmt(lag['p99'] if lag else None, 0)} | {'yes' if sustained(r) else 'NO'} |"
        )

    lines += [
        "",
        f"Payload of one batch of 100 views: JSON {sizes['json']:,} B, Protobuf {sizes['protobuf']:,} B"
        f" ({sizes['json'] / sizes['protobuf']:.1f}x smaller).",
        "",
        "## 2. One view per call (unary): the ceiling of each protocol",
        "",
        "| Protocol | processes | target RPS | achieved RPS | failed | dropped | view p50 ms | view p99 ms"
        " | kept up |",
        "|---|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for r in sorted(unary, key=lambda r: (r["workers"], r["protocol"], r["target_increments_per_s"])):
        v = r["view_call_ms"]
        lines.append(
            f"| {name[r['protocol']]} | {r['workers']} | {r['target_increments_per_s']:,}"
            f" | {fmt(r['achieved_increments_per_s'], 0)} | {r['failed_calls']} | {r['dropped_calls']:,}"
            f" | {fmt(v['p50'] if v else None)} | {fmt(v['p99'] if v else None)}"
            f" | {'yes' if sustained(r) else 'NO'} |"
        )

    lines += ["", "Highest rate that each setup kept up with:", ""]
    lines += ["| Protocol | processes | max kept-up RPS | per process |", "|---|---:|---:|---:|"]
    for workers in sorted({r["workers"] for r in unary}):
        for protocol in PROTOCOLS:
            ok = [
                r["target_increments_per_s"]
                for r in unary
                if r["workers"] == workers and r["protocol"] == protocol and sustained(r)
            ]
            best = max(ok) if ok else 0
            lines.append(f"| {name[protocol]} | {workers} | {best:,} | {best // workers:,} |")

    lines += ["", "![Load test](load_chart.png)", ""]
    (ROOT / "benchmark" / "load_results.md").write_text("\n".join(lines))
    draw_chart(unary)
    print("wrote benchmark/load_results.md and benchmark/load_chart.png")


def draw_chart(unary: list[dict]) -> None:
    from benchmark.charts import COLORS, GRID, MUTED, SURFACE, TEXT, plt, style_axis

    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4.6), facecolor=SURFACE)
    style_axis(left, "Achieved vs target rate (unary)", "achieved RPS")
    style_axis(right, "p99 latency of a view (unary)", "p99, ms (log scale)")
    top = max(r["target_increments_per_s"] for r in unary)
    left.plot([0, top], [0, top], color=GRID, linewidth=1.5, linestyle=":")  # "perfect" line
    left.text(top, top, " target", color=MUTED, va="center", fontsize=9)
    right.axhline(SLO_P99_MS, color=MUTED, linewidth=1, linestyle=":")
    right.text(0, SLO_P99_MS, f" SLO {SLO_P99_MS} ms", color=MUTED, va="bottom", fontsize=9)
    right.set_yscale("log")

    for workers, style, marker in ((4, "-", "o"), (1, "--", "s")):
        for protocol, label in (("rest", "REST"), ("grpc", "gRPC")):
            rows = sorted(
                (r for r in unary if r["workers"] == workers and r["protocol"] == protocol),
                key=lambda r: r["target_increments_per_s"],
            )
            if not rows:
                continue
            x = [r["target_increments_per_s"] for r in rows]
            series = f"{label}, {workers} proc"
            kw = {
                "color": COLORS[label],
                "linestyle": style,
                "marker": marker,
                "markersize": 6,
                "linewidth": 2,
            }
            left.plot(x, [r["achieved_increments_per_s"] for r in rows], label=series, **kw)
            right.plot(x, [r["view_call_ms"]["p99"] for r in rows], label=series, **kw)
            left.annotate(
                series,
                (x[-1], rows[-1]["achieved_increments_per_s"]),
                xytext=(6, 0),
                textcoords="offset points",
                color=TEXT,
                fontsize=8,
                va="center",
            )

    for ax in (left, right):
        ax.set_xlabel("target RPS", color=MUTED)
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v / 1000:.0f}K"))
    left.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v / 1000:.0f}K"))
    right.legend(frameon=False, labelcolor=TEXT, fontsize=9, loc="upper left")
    fig.tight_layout()
    fig.savefig(ROOT / "benchmark" / "load_chart.png", dpi=150, facecolor=SURFACE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="short runs, to check that it works")
    parser.add_argument("--report", action="store_true", help="only rebuild the report from saved JSON")
    args = parser.parse_args()
    if not args.report:
        started = time.time()
        run_matrix(args.quick)
        print(f"matrix done in {(time.time() - started) / 60:.1f} min")
    write_report()
