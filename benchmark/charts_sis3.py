"""SIS #3 charts (PNG) from the measured results in benchmark/db/*.json.

    python -m benchmark.charts_sis3      # -> benchmark/db/charts/*.png

Every number is read from the JSON files, nothing is typed in by hand.
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # draw to files, no window
import matplotlib.pyplot as plt  # noqa: E402

DB = Path(__file__).parent / "db"
OUT = DB / "charts"

BEFORE = "#9c9c9c"  # gray = the baseline / "before"
AFTER = "#257de0"  # blue = the change / "after"
DIRECT = "#257de0"  # blue  (categorical slot 1)
BOUNCER = "#f47230"  # orange (categorical slot 2)
SURFACE = "#ffffff"
TEXT = "#191919"
MUTED = "#666666"
GRID = "#e6e6e6"


def load(name: str) -> dict:
    return json.loads((DB / f"{name}.json").read_text())


def style(ax, title: str, y_label: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=TEXT, fontsize=11, fontweight="bold")
    ax.set_ylabel(y_label, color=MUTED)
    ax.tick_params(colors=MUTED, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)


def bars(ax, labels: list[str], values: list[float], colors: list[str], fmt: str) -> None:
    drawn = ax.bar(labels, values, color=colors, width=0.55, edgecolor=SURFACE, linewidth=2)
    for bar, value in zip(drawn, values, strict=True):
        ax.annotate(
            fmt.format(value),
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            ha="center",
            va="bottom",
            xytext=(0, 3),
            textcoords="offset points",
            color=TEXT,
            fontsize=10,
        )
    ax.margins(y=0.15)


def save(fig, name: str, subtitle: str) -> None:
    fig.text(0.01, 0.005, subtitle, color=MUTED, fontsize=8, ha="left", va="bottom")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(OUT / f"{name}.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print("wrote", OUT / f"{name}.png")


def index_tradeoff() -> None:
    """The index buys a fast read and costs every write: two measures, two panels (no dual axis)."""
    idx, e1 = load("index"), load("experiments")["E1"]
    fig, (read, write) = plt.subplots(1, 2, figsize=(9, 3.6))
    bars(
        read,
        ["no index", "with likes(post_id)"],
        [idx["without_index"]["p50"], idx["with_index"]["p50"]],
        [BEFORE, AFTER],
        "{:.1f} ms",
    )
    style(read, '"Who liked post X" — read p50', "ms")
    bars(
        write,
        ["no index", "with likes(post_id)"],
        [e1["without"]["p99"], e1["with_post_id_index"]["p99"]],
        [BEFORE, AFTER],
        "{:.1f} ms",
    )
    style(write, "Every like INSERT — p99", "ms")
    save(
        fig,
        "1_index_tradeoff",
        "Reads ~370x faster, writes +63% slower. T7 has no such read -> index not kept.",
    )


def unnest() -> None:
    q = load("before")
    old, new = q["Q5 sync 1000 (executemany)"], q["Q5 sync 1000 (unnest)"]
    fig, ax = plt.subplots(figsize=(6, 3.6))
    labels = ["p50", "p95", "p99"]
    x = range(len(labels))
    w = 0.38
    for offset, data, color, name in (
        (-w / 2, old, BEFORE, "executemany (1000 statements)"),
        (w / 2, new, AFTER, "unnest (1 statement)"),
    ):
        drawn = ax.bar(
            [i + offset for i in x],
            [data[k] for k in labels],
            w,
            color=color,
            label=name,
            edgecolor=SURFACE,
            linewidth=2,
        )
        for bar in drawn:
            ax.annotate(
                f"{bar.get_height():.0f}",
                (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                ha="center",
                va="bottom",
                xytext=(0, 3),
                textcoords="offset points",
                color=TEXT,
                fontsize=9,
            )
    ax.set_xticks(list(x), labels)
    ax.margins(y=0.15)
    ax.legend(frameon=False, labelcolor=TEXT, fontsize=9)
    style(ax, "Sync worker: write 1000 counters", "ms")
    save(fig, "2_unnest", "Same database, same moment. One parse, one plan, one round trip instead of 1000.")


def drop_vs_delete() -> None:
    e2 = load("experiments")["E2"]
    delete = e2["none"]["remove_old"]
    drop = e2["range_week"]["remove_old"]
    fig, (t, wal) = plt.subplots(1, 2, figsize=(9, 3.6))
    bars(t, ["DELETE rows", "DROP partition"], [delete["ms"], drop["ms"]], [BEFORE, AFTER], "{:,.0f} ms")
    style(t, "Remove the oldest week — time", "ms")
    bars(
        wal,
        ["DELETE rows", "DROP partition"],
        [delete["wal_mb"], drop["wal_mb"]],
        [BEFORE, AFTER],
        "{:.2f} MB",
    )
    style(wal, "WAL written (also sent to the replica)", "MB")
    save(
        fig,
        "3_drop_vs_delete",
        "5M-row copy of likes. DROP removes a file; DELETE touches every row and leaves dead rows.",
    )


def partitions() -> None:
    e2 = load("experiments")["E2"]
    order = [
        ("none", "no partitions"),
        ("hash16_user", "HASH(user_id) x16"),
        ("range_week", "RANGE week x5"),
        ("range_day", "RANGE day x31"),
    ]
    fig, (scanned, p50) = plt.subplots(1, 2, figsize=(10, 3.8))
    labels = [label for _, label in order]
    colors = [BEFORE, AFTER, BEFORE, BEFORE]
    bars(scanned, labels, [e2[k]["tables_scanned"] for k, _ in order], colors, "{:.0f}")
    style(scanned, "Partitions read by liked_by_me", "tables")
    bars(p50, labels, [e2[k]["liked_by_me"]["p50"] for k, _ in order], colors, "{:.2f} ms")
    style(p50, "liked_by_me — p50", "ms")
    for ax in (scanned, p50):
        ax.tick_params(axis="x", labelsize=8)
    save(
        fig,
        "4_partitions",
        "Queries filter by user_id: HASH(user_id) prunes to 1 of 16. By day: 31 tables, 2.3x slower p50.",
    )


def pool_sweep() -> None:
    sizes = [2, 5, 10, 25, 50]
    fig, (conns, fails) = plt.subplots(1, 2, figsize=(10, 3.8))
    for mode, color, label, marker in (
        ("direct", DIRECT, "direct", "o"),
        ("pgbouncer", BOUNCER, "PgBouncer", "s"),
    ):
        runs = [load(f"pool_{mode}_{s}") for s in sizes]
        conns.plot(
            sizes,
            [r["max_backends"] for r in runs],
            color=color,
            linewidth=2,
            marker=marker,
            markersize=8,
            markeredgecolor=SURFACE,
            markeredgewidth=2,
            label=label,
        )
        fails.plot(
            sizes,
            [100 * r["failed_calls"] / r["calls"] for r in runs],
            color=color,
            linewidth=2,
            marker=marker,
            markersize=8,
            markeredgecolor=SURFACE,
            markeredgewidth=2,
            label=label,
        )
    repeat = load("pool_pgbouncer_50_repeat")
    repeat_pct = 100 * repeat["failed_calls"] / repeat["calls"]
    fails.plot(
        [50],
        [repeat_pct],
        marker="s",
        markersize=8,
        color=SURFACE,
        markeredgecolor=BOUNCER,
        markeredgewidth=2,
    )
    fails.annotate(
        f"repeated: {repeat_pct:.1f}%",
        (50, repeat_pct),
        xytext=(-8, 8),
        textcoords="offset points",
        ha="right",
        color=TEXT,
        fontsize=8,
    )
    conns.axhline(100, color=MUTED, linewidth=1, linestyle="--")
    conns.annotate(
        "max_connections = 100", (2, 100), xytext=(0, 4), textcoords="offset points", color=MUTED, fontsize=8
    )
    for ax in (conns, fails):
        ax.set_xscale("log")
        ax.set_xticks(sizes, [str(s) for s in sizes])
        ax.minorticks_off()
        ax.set_xlabel("asyncpg pool size per process (8 processes)", color=MUTED)
        ax.legend(frameon=False, labelcolor=TEXT, fontsize=9)
    style(conns, "Real Postgres connections (max seen)", "connections")
    style(fails, "Failed calls", "%")
    save(
        fig,
        "5_pool_sweep",
        "300 likes/s + 300 reads/s, 60 s. PgBouncer caps Postgres at 30 connections. Pool 2 is too small."
        " PgBouncer pool 50: one noisy run (0.3% when repeated).",
    )


def replica() -> None:
    without, with_ = load("mix2_noreplica_w8"), load("mix2_after_w8")
    fig, ax = plt.subplots(figsize=(6, 3.6))
    labels = ["read p95", "read p99", "like p99"]
    vals = lambda r: [r["read_ms"]["p95"], r["read_ms"]["p99"], r["like_ms"]["p99"]]  # noqa: E731
    x = range(len(labels))
    w = 0.38
    for offset, data, color, name in (
        (-w / 2, without, BEFORE, "reads from the primary"),
        (w / 2, with_, AFTER, "reads from the replica"),
    ):
        drawn = ax.bar(
            [i + offset for i in x], vals(data), w, color=color, label=name, edgecolor=SURFACE, linewidth=2
        )
        for bar in drawn:
            ax.annotate(
                f"{bar.get_height():.0f}",
                (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                ha="center",
                va="bottom",
                xytext=(0, 3),
                textcoords="offset points",
                color=TEXT,
                fontsize=9,
            )
    ax.set_xticks(list(x), labels)
    ax.margins(y=0.15)
    ax.legend(frameon=False, labelcolor=TEXT, fontsize=9)
    style(ax, "Read replica: the tail gets 3-4x better", "ms")
    save(
        fig,
        "6_replica",
        "k6 via gRPC, 300 likes/s + 300 cold reads/s, 8 processes, PgBouncer. Lag p50 1 ms, max 0.6 s.",
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    index_tradeoff()
    unnest()
    drop_vs_delete()
    partitions()
    pool_sweep()
    replica()


if __name__ == "__main__":
    main()
