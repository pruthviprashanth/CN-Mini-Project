#!/usr/bin/env python3
"""Plots from results/*.csv into results/plots/ (needs matplotlib, no root).

  python3 experiments/plot.py          # or: uv run --with matplotlib experiments/plot.py
"""
import csv
import json
import os
import statistics
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")
PLOTS = os.path.join(RESULTS, "plots")

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SDN, STATIC, THIRD = "#2a78d6", "#eb6834", "#1baf7a"     # categorical slots 1-3
VARIANT_COLOR = {"static": STATIC, "sdn-nomonitor": THIRD, "sdn-monitor": SDN}
VARIANT_LABEL = {"static": "static baseline", "sdn-nomonitor": "SDN, monitor off",
                 "sdn-monitor": "SDN, monitor on"}

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "axes.titlecolor": INK,
    "xtick.color": INK2, "ytick.color": INK2, "text.color": INK, "font.size": 10,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False, "lines.linewidth": 2,
    "legend.frameon": False, "axes.titlesize": 11, "axes.titleweight": "bold",
})


def read(name):
    path = os.path.join(RESULTS, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return list(csv.DictReader(f))


def num(x):
    return float(x) if x not in (None, "", "None") else None


def save(fig, name):
    os.makedirs(PLOTS, exist_ok=True)
    fig.savefig(os.path.join(PLOTS, name), dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote results/plots/%s" % name)


def latency_vs_users(rows):
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8), sharey=True)
    for ax, bulk, title in ((axes[0], "False", "No background traffic"),
                            (axes[1], "True", "With bulk TCP congestion (4 streams)")):
        for mode, color, label in (("static", STATIC, "static baseline"), ("sdn", SDN, "SDN")):
            pts = sorted((int(r["users_n"]), num(r["rtt_p50"]), num(r["rtt_p95"]))
                         for r in rows if r["mode"] == mode and r["bulk"] == bulk)
            if not pts:
                continue
            xs = [p[0] for p in pts]
            ax.plot(xs, [p[2] for p in pts], color=color, marker="o", markersize=6,
                    label=label + " p95")
            ax.plot(xs, [p[1] for p in pts], color=color, marker="o", markersize=4,
                    linestyle="--", linewidth=1.5, label=label + " p50")
            ax.annotate("%.1f ms" % pts[-1][2], (xs[-1], pts[-1][2]), textcoords="offset points",
                        xytext=(6, 0), va="center", color=INK2, fontsize=9)
        ax.set_title(title, loc="left")
        ax.set_xlabel("concurrent users")
        ax.set_yscale("log")
        ax.set_xticks(sorted({int(r["users_n"]) for r in rows}))
    axes[0].set_ylabel("message RTT, send → ack (ms, log)")
    axes[1].legend(loc="center left", bbox_to_anchor=(1.12, 0.5), fontsize=9)
    fig.suptitle("Chat response time vs number of users", x=0.01, ha="left", fontweight="bold")
    save(fig, "latency_vs_users.png")


def bulk_throughput(rows):
    pts = defaultdict(dict)
    for r in rows:
        if r["bulk"] == "True" and num(r.get("bulk_mbps")):
            pts[int(r["users_n"])][r["mode"]] = num(r["bulk_mbps"])
    if not pts:
        return
    ns = sorted(pts)
    fig, ax = plt.subplots(figsize=(6, 3.4))
    w = 0.38
    for i, (mode, color, label) in enumerate((("static", STATIC, "static baseline"), ("sdn", SDN, "SDN"))):
        xs = [k + (i - 0.5) * (w + 0.04) for k in range(len(ns))]
        vals = [pts[n].get(mode, 0) for n in ns]
        ax.bar(xs, vals, width=w, color=color, label=label)
        for x, v in zip(xs, vals):
            ax.text(x, v + 0.15, "%.1f" % v, ha="center", fontsize=8, color=INK2)
    ax.set_xticks(range(len(ns)))
    ax.set_xticklabels([str(n) for n in ns])
    ax.set_xlabel("concurrent chat users")
    ax.set_ylabel("bulk throughput (Mbps)")
    ax.set_ylim(0, 11)
    ax.axhline(10, color=INK2, linewidth=1, linestyle=":")
    ax.text(len(ns) - 0.5, 10.15, "10 Mbps trunk", ha="right", fontsize=8, color=INK2)
    ax.legend(loc="lower right", fontsize=9)
    ax.set_title("Bulk traffic is not starved by prioritizing chat", loc="left")
    save(fig, "bulk_throughput.png")


def flood_timeline(series, meta):
    bins = defaultdict(list)
    for r in series:
        bins[(r["variant"], r["group"], int(float(r["t"])))].append(float(r["rtt_ms"]))
    variants = [v for v in ("static", "sdn-nomonitor", "sdn-monitor") if any(k[0] == v for k in bins)]
    fig, axes = plt.subplots(len(variants), 1, figsize=(9, 2.3 * len(variants)), sharex=True, sharey=True)
    if len(variants) == 1:
        axes = [axes]
    T = int(meta["duration"])
    for ax, v in zip(axes, variants):
        ax.axvspan(meta["flood_start"], meta["flood_end"], color="#f1efe9", zorder=0)
        for group, color in (("regular", STATIC), ("vip", SDN)):
            xs, ys = [], []
            for t in range(T):
                vals = bins.get((v, group, t))
                if vals:
                    xs.append(t + 0.5)
                    ys.append(statistics.median(vals))
            ax.plot(xs, ys, color=color, label="%s users (median per 1 s)" % group)
        ax.set_yscale("log")
        ax.set_title(VARIANT_LABEL[v], loc="left")
        ax.set_ylabel("RTT (ms, log)")
    axes[0].text(meta["flood_start"] + 0.3, axes[0].get_ylim()[0] * 1.3, "flood active",
                 fontsize=8, color=INK2, va="bottom")
    axes[0].legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel("time since start (s)")
    fig.suptitle("One client floods the server: victims' response time over time",
                 x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    save(fig, "flood_timeline.png")


def vip_vs_regular(series, meta):
    """p95 RTT of messages sent while the flood was active."""
    vals = defaultdict(list)
    for r in series:
        t = float(r["t"])
        if meta["flood_start"] <= t < meta["flood_end"]:
            vals[(r["variant"], r["group"])].append(float(r["rtt_ms"]))
    variants = [v for v in ("static", "sdn-nomonitor", "sdn-monitor") if (v, "vip") in vals]
    if not variants:
        return

    def p95(x):
        x = sorted(x)
        return x[int(0.95 * (len(x) - 1))]

    fig, ax = plt.subplots(figsize=(7, 3.6))
    w = 0.38
    for i, (group, color) in enumerate((("vip", SDN), ("regular", STATIC))):
        xs = [k + (i - 0.5) * (w + 0.04) for k in range(len(variants))]
        ys = [p95(vals[(v, group)]) for v in variants]
        ax.bar(xs, ys, width=w, color=color, label=group + " users")
        for x, y in zip(xs, ys):
            ax.text(x, y * 1.15, "%.1f" % y, ha="center", fontsize=8, color=INK2)
    ax.set_yscale("log")
    ax.set_xticks(range(len(variants)))
    ax.set_xticklabels([VARIANT_LABEL[v] for v in variants])
    ax.set_ylabel("p95 RTT during flood (ms, log)")
    ax.legend(loc="upper right", fontsize=9)
    ax.set_title("VIP vs regular users while a client floods", loc="left")
    save(fig, "vip_vs_regular.png")


def link_failure(rows, rob):
    if not rows or not rob:
        return
    lf = rob["link_failure"]
    fig, ax = plt.subplots(figsize=(8, 3.2))
    ax.axvspan(lf["down_t"], lf["up_t"], color="#f1efe9", zorder=0)
    ax.scatter([float(r["t"]) for r in rows], [float(r["rtt_ms"]) for r in rows], s=10,
               color=SDN, edgecolors="none")
    ax.set_yscale("log")
    ax.set_xlabel("message send time (s)")
    ax.set_ylabel("RTT (ms, log)")
    ax.text(lf["down_t"] + 0.1, ax.get_ylim()[0] * 1.3, "trunk down", fontsize=8, color=INK2, va="bottom")
    ax.set_title("Trunk link down for 3 s: TCP retransmits, no message lost (%d sent, %d unacked)"
                 % (lf["summary"]["sent"], lf["summary"]["unacked"]), loc="left")
    save(fig, "link_failure.png")


def main():
    rows = read("scaling.csv")
    if rows:
        latency_vs_users(rows)
        bulk_throughput(rows)
    series = read("flood_timeseries.csv")
    meta_path = os.path.join(RESULTS, "flood_meta.json")
    if series and os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        flood_timeline(series, meta)
        vip_vs_regular(series, meta)
    rob_path = os.path.join(RESULTS, "robustness.json")
    if os.path.exists(rob_path):
        with open(rob_path) as f:
            link_failure(read("link_failure_timeseries.csv"), json.load(f))


if __name__ == "__main__":
    main()
