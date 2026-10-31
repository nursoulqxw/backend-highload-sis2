"""Two PNG charts for the benchmark results (matplotlib)."""

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # draw to files, no window
import matplotlib.pyplot as plt  # noqa: E402

COLORS = {"REST": "#2a78d6", "gRPC": "#eb6834"}  # blue / orange, easy to tell apart for colorblind people
HATCH = {"REST": "", "gRPC": "//"}  # a second hint besides color
SURFACE = "#fcfcfb"
TEXT = "#2b2b29"
MUTED = "#8a8a84"
GRID = "#e4e4df"
BAR_WIDTH = 0.38


def style_axis(ax, title: str, y_label: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=TEXT, fontsize=12, fontweight="bold")
    ax.set_ylabel(y_label, color=MUTED)
    ax.tick_params(colors=MUTED, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)


def grouped_bars(ax, rows: list[dict], operations: list[str], field: str, label_format: str) -> None:
    """One group per operation, one bar per protocol, with the value on top of each bar."""
    for offset, protocol in ((-BAR_WIDTH / 2, "REST"), (BAR_WIDTH / 2, "gRPC")):
        values = [
            next(r[field] for r in rows if r["protocol"] == protocol and r["operation"] == op)
            for op in operations
        ]
        positions = [i + offset for i in range(len(operations))]
        bars = ax.bar(
            positions,
            values,
            BAR_WIDTH,
            label=protocol,
            color=COLORS[protocol],
            hatch=HATCH[protocol],
            edgecolor=SURFACE,  # 2 px gap between bars
            linewidth=2,
        )
        ax.bar_label(bars, labels=[label_format.format(v) for v in values], padding=3, color=TEXT, fontsize=8)
    tick_labels = [op.replace(" ", "\n") for op in operations]  # "GetCounters\nx100": no overlap
    ax.set_xticks(range(len(operations)), tick_labels, color=TEXT)
    ax.legend(frameon=False, loc="upper left", labelcolor=TEXT)


def draw_latency_chart(rows: list[dict], operations: list[str], path: Path, slo_ms: float) -> None:
    fig, (ax_p50, ax_p99) = plt.subplots(1, 2, figsize=(14, 5), facecolor=SURFACE)
    grouped_bars(ax_p50, rows, operations, "p50_ms", "{:.1f}")
    style_axis(ax_p50, "p50 latency (typical request) - lower is better", "milliseconds")
    grouped_bars(ax_p99, rows, operations, "p99_ms", "{:.1f}")
    style_axis(ax_p99, "p99 latency (1 in 100 slowest) - lower is better", "milliseconds")
    if max(r["p99_ms"] for r in rows) > slo_ms * 0.5:  # show the SLO line only if it is in range
        ax_p99.axhline(slo_ms, color=MUTED, linestyle="--", linewidth=1)
        ax_p99.text(-0.45, slo_ms, f"SLO: p99 < {slo_ms:g} ms", va="bottom", color=MUTED, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def draw_payload_chart(rows: list[dict], operations: list[str], path: Path) -> None:
    # LikePost / RecordView have empty responses (0 B) in both protocols, so the right side skips them.
    get_counters = [op for op in operations if op.startswith("GetCounters")]
    fig, (ax_request, ax_response) = plt.subplots(1, 2, figsize=(14, 5), facecolor=SURFACE)
    grouped_bars(ax_request, rows, operations, "request_bytes", "{:,} B")
    style_axis(ax_request, "Request size: the same data - lower is better", "bytes")
    grouped_bars(ax_response, rows, get_counters, "response_bytes", "{:,} B")
    style_axis(ax_response, "GetCounters response size - lower is better", "bytes")
    fig.text(
        0.01,
        0.01,
        "REST request = the same fields as JSON (they really travel in the URL and a header). "
        "LikePost / RecordView: 0 B response in both protocols.",
        color=MUTED,
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def draw_from_csv(results_dir: Path, slo_ms: float = 100) -> None:
    """Redraw both charts from results.csv without running the benchmark again."""
    with (results_dir / "results.csv").open() as file:
        rows = list(csv.DictReader(file))
    for row in rows:  # CSV gives strings, the charts need numbers
        row["p50_ms"] = float(row["p50_ms"])
        row["p99_ms"] = float(row["p99_ms"])
        row["request_bytes"] = int(row["request_bytes"])
        row["response_bytes"] = int(row["response_bytes"])
    operations = list(dict.fromkeys(row["operation"] for row in rows))
    draw_latency_chart(rows, operations, results_dir / "latency_p50_p99.png", slo_ms)
    draw_payload_chart(rows, operations, results_dir / "payload_size.png")


if __name__ == "__main__":
    draw_from_csv(Path(__file__).parent)  # python -m benchmark.charts
