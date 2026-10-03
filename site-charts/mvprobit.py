"""Site chart for quantecarlo.com/multivariate-probit: python site-charts/mvprobit.py OUT_DIR.

Numbers are copied from github.com/sign-of-fourier/multivariate-probit docs/studies/ghk.md (pairwise-shaped
correlation matrix, seconds per row) and docs/studies/comparators.md (quadrature, which does not depend on the matrix)."""
import sys
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker

OUT = Path(sys.argv[1])
BG, LINE, TEXT, MUTED, ACC = "#0b1020", "#26325a", "#e8ecf7", "#9aa6c8", "#5ee3c8"
D = [4, 6, 10, 14, 20]
SERIES = [  # (label, d values, seconds per row, color, linestyle)
    ("SciPy", D, [6.4e-3, 1.1, 2.1, 0.82, 0.45], "#7c89b0", "-"),
    ("Exact quadrature", [3, 4, 5, 6, 7], [8e-5, 9e-4, 1e-2, 0.12, 58], "#7c89b0", "--"),
    ("GHK, 1000 draws", D, [3.0e-4, 4.4e-4, 7.7e-4, 1.3e-3, 1.7e-3], "#b8a7e8", "-"),
    ("GHK, 100 draws", D, [5.1e-5, 8.2e-5, 1.4e-4, 2.6e-4, 3.6e-4], "#b8a7e8", "--"),
    ("Compiled orthant", D, [1.6e-5, 2.3e-5, 3.1e-5, 5.8e-5, 7.9e-5], ACC, "-"),
]
plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": TEXT, "axes.labelcolor": MUTED})
fig, ax = plt.subplots(figsize=(10.4, 5.2), facecolor=BG)
fig.subplots_adjust(left=0.09, right=0.78, top=0.80, bottom=0.12)
ax.set_facecolor(BG)
fig.text(0.02, 0.93, "Seconds to score one row, by number of outcomes", fontsize=15, weight="bold", color=TEXT)
fig.text(0.02, 0.865, "P(Y = y | x) for the observed pattern · log scale · lower is faster", fontsize=10.5, color=MUTED)
for s in ("top", "right", "left"):
    ax.spines[s].set_visible(False)
ax.spines["bottom"].set_color(LINE)
ax.tick_params(colors=MUTED, labelsize=10, length=0)
ax.grid(axis="y", color=LINE, linewidth=0.8)
ax.set_axisbelow(True)
ax.set_yscale("log")
ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
ax.set_ylim(5e-6, 200)
ax.set_xlim(2.5, 20.5)
ax.set_xticks([3, 4, 6, 7, 10, 14, 20])
ax.set_xlabel("outcomes (d)")
ax.set_yticks([1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1, 10, 100])
ax.set_yticklabels(["10 µs", "100 µs", "1 ms", "10 ms", "0.1 s", "1 s", "10 s", "100 s"])
for label, xs, ys, col, ls in SERIES:
    hero = col == ACC
    ax.plot(xs, ys, color=col, linestyle=ls, linewidth=2.6 if hero else 2, marker="o", markersize=5,
            markeredgecolor=BG, markeredgewidth=1.5, zorder=3 if hero else 2)
    ax.annotate(label, (xs[-1], ys[-1]), xytext=(10, 0), textcoords="offset points", va="center",
                fontsize=10.5, color=TEXT if hero else MUTED, weight="bold" if hero else "normal",
                annotation_clip=False)
fig.savefig(OUT / "mvprobit-speed.png", dpi=150, facecolor=BG)
print("wrote", OUT / "mvprobit-speed.png")
