"""Site charts for the OSHA study (quantecarlo.com Findings): python site-charts/osha.py DATA_DIR OUT_DIR, where DATA_DIR
holds evaluations.jsonl and reference_scores.json from bpto experiments/2026-09-26-osha-sir-gepa-vs-qei (at ccc8892)."""
import json, sys
from pathlib import Path
from statistics import mean
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE, OUT = Path(sys.argv[1]), Path(sys.argv[2])
runs = [json.loads(l) for l in (HERE / "evaluations.jsonl").read_text().splitlines()]
ref = json.loads((HERE / "reference_scores.json").read_text())["holdout"]
ho = lambda r: mean(x["acc"] for x in r["reps"])
BG, CARD, LINE, TEXT, MUTED, ACC, ACC2 = "#0b1020", "#161f3d", "#26325a", "#e8ecf7", "#9aa6c8", "#5ee3c8", "#ffb454"
ARMS = [("q1", "GEPA, one rewrite at a time"), ("independent4", "GEPA, four at a time (its own parallel mode)"),
        ("qei4", "bpto, four at a time")]
COL = {"q1": "#7c89b0", "independent4": "#7c89b0", "qei4": ACC}
plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": TEXT, "axes.labelcolor": MUTED})


def frame(title, sub):
    fig, ax = plt.subplots(figsize=(10.4, 4.0), facecolor=BG)
    ax.set_facecolor(BG)
    fig.text(0.02, 0.93, title, fontsize=15, weight="bold", color=TEXT)
    fig.text(0.02, 0.865, sub, fontsize=10.5, color=MUTED)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(LINE)
    ax.tick_params(colors=MUTED, labelsize=10, length=0)
    ax.grid(axis="x", color=LINE, linewidth=0.8)
    ax.set_axisbelow(True)
    return fig, ax


def rows(ax, get, fmt, bars, label_x):
    """bars=True for time (starts at zero); False for accuracy: dots and a mean tick, no bar, since a bar on an axis
    that does not start at zero exaggerates the gaps."""
    ys = list(range(len(ARMS)))[::-1]
    for y, (arm, label) in zip(ys, ARMS):
        vals = [get(r) for r in runs if r["arm"] == arm]
        m = mean(vals)
        jit = [y + (j - 2) * 0.07 for j in range(len(vals))]
        if bars:
            ax.barh(y, m, height=0.52, color=COL[arm], alpha=0.9 if arm == "qei4" else 0.55, zorder=2)
            ax.scatter(vals, [y] * len(vals), s=24, color=TEXT, edgecolor=BG, linewidth=1, zorder=3)
        else:
            ax.hlines(y, min(vals), max(vals), color=COL[arm], linewidth=1.2, alpha=0.6, zorder=2)
            ax.scatter(vals, jit, s=34, color=COL[arm], edgecolor=BG, linewidth=1, zorder=3)
            ax.vlines(m, y - 0.3, y + 0.3, color=COL[arm], linewidth=3.5, zorder=4)
        ax.text(label_x, y, fmt(m), transform=ax.get_yaxis_transform(), va="center", fontsize=12.5, weight="bold",
                color=ACC if arm == "qei4" else TEXT)
    ax.set_yticks(ys, [l for _, l in ARMS], fontsize=11, color=TEXT)
    return ys


# 1. wall clock, in minutes
fig, ax = frame("Same 5,000 calls, same bill: time to finish",
                "Minutes per run. GEPA = the official package, the optimizer DSPy uses. Bar = average of 5 runs, dots = each run.")
rows(ax, lambda r: r["secs"] / 60, lambda m: f"{m:.1f} min", True, 1.01)
ax.set_xlim(0, 13)
ax.set_xlabel("minutes", fontsize=10)
fig.text(0.02, 0.02, "OSHA injury reports, 13 categories · Nova Micro answers, Nova Lite rewrites · 16 calls in flight · "
         "source: bpto experiments/2026-09-26-osha-sir-gepa-vs-qei", fontsize=8.5, color=MUTED)
fig.subplots_adjust(left=0.34, right=0.9, top=0.78, bottom=0.2)
fig.savefig(OUT / "osha-wallclock.png", dpi=150, facecolor=BG)

# 2. accuracy on held-out reports, with the reference prompts
fig, ax = frame("…and the answers were just as good",
                "Accuracy on 200 held-out reports. Thick tick = average of 5 runs, dots = each run. Dashed: no search at all.")
rows(ax, lambda r: ho(r["holdout"]), lambda m: f"{m*100:.1f}%", False, 1.01)
for key, label, color, dy in [("B2", "starting prompt", MUTED, 1), ("onestep_strong", "one rewrite by Opus 5.5", ACC2, -1)]:
    v = ho(ref[key])
    ax.axvline(v, color=color, linestyle=(0, (4, 3)), linewidth=1.4, zorder=1)
    ax.annotate(f"{label} {v*100:.1f}%", (v, 2.45 if dy > 0 else -0.55), xytext=(4, 0), textcoords="offset points",
                fontsize=9.5, color=color, va="center")
ax.set_xlim(0.60, 0.74)
ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{x*100:.0f}%"))
ax.set_ylim(-0.8, 2.75)
fig.text(0.02, 0.02, "On average no setup beat the starting prompt: the speed-up cost no accuracy · source: bpto experiments/2026-09-26-osha-sir-gepa-vs-qei", fontsize=8.5, color=MUTED)
fig.subplots_adjust(left=0.34, right=0.9, top=0.78, bottom=0.2)
fig.savefig(OUT / "osha-accuracy.png", dpi=150, facecolor=BG)
for q in ("q1", "independent4", "qei4"):
    rs = [r for r in runs if r["arm"] == q]
    print(q, round(mean(r["secs"] for r in rs)), round(mean(ho(r["holdout"]) for r in rs), 3), round(mean(r["spent_usd"] for r in rs), 3))
print("B2", ho(ref["B2"]), "opus", ho(ref["onestep_strong"]))
