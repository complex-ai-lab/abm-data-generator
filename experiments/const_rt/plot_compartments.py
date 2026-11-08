"""Small multiples of the constant-R_t runs: one row per county, one column per compartment
(S, E, I, R as % of the county population, mean of the iterations), plus reported cases vs observed.
Colors = the 3 R_t settings of the county (mean / draw1 / draw2). V is always 0 (no vaccination).

Run from the repo root:  .venv/bin/python experiments/const_rt/plot_compartments.py
"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D

MANIFEST = "experiments/const_rt/manifest.csv"
OUT = "experiments/const_rt/const_rt_compartments.png"

# reference palette, categorical slots 1-3 (validated all-pairs, light mode)
COLORS = {"mean": "#2a78d6", "draw1": "#eb6834", "draw2": "#1baf7a"}
INK, INK2, GRID, BURN = "#0b0b0b", "#52514e", "#e4e3df", "#f1f0ec"
COLS = [("S", "S (% of pop.)"), ("E", "E (% of pop.)"), ("I", "I (% of pop.)"),
        ("R", "R (% of pop.)"), ("cases", "Reported cases / day")]

man = pd.read_csv(MANIFEST, dtype={"fips": str})
counties = list(dict.fromkeys(man["fips"]))
plt.rcParams.update({"font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
                     "xtick.color": INK2, "ytick.color": INK2, "text.color": INK})
fig, axes = plt.subplots(len(counties), len(COLS), figsize=(18, 2.3 * len(counties)),
                         sharex=True, squeeze=False)

for r, fips in enumerate(counties):
    sub = man[man["fips"] == fips]
    label = []
    for row in sub.itertuples():
        d = f"results/{fips}/const_rt_{row.setting}"
        s = pd.read_csv(f"{d}/seirv_daily.csv", parse_dates=["date"])
        c = pd.read_csv(f"{d}/cases_sim_vs_observed.csv", parse_dates=["date"])
        n = s[["S", "E", "I", "R", "V"]].iloc[0].sum()
        for j, (col, _) in enumerate(COLS[:4]):
            axes[r, j].plot(s["date"], 100 * s[col] / n, color=COLORS[row.setting], lw=1.6,
                            solid_capstyle="round")
        axes[r, 4].plot(c["date"], c["simulated_cases"], color=COLORS[row.setting], lw=1.6)
        label.append(f"{row.r_t:.2f}")
        if row.setting == "mean":
            obs, burn_end = c[["date", "observed_cases"]], c["date"].iloc[0]
            burn_start = s["date"].iloc[0]
    axes[r, 4].plot(obs["date"], obs["observed_cases"], color=INK2, lw=0.8, alpha=0.6, zorder=0)
    i0 = sub["i0"].iloc[0]
    axes[r, 0].set_ylabel(f"{fips}\nR_t {' / '.join(label)}\n(mean / draw1 / draw2)"
                          f"\nI0 {100 * i0:.2f}%", rotation=0, ha="right", va="center",
                          labelpad=10, color=INK, fontsize=8.5)
    for ax in axes[r]:
        ax.axvspan(burn_start, burn_end, color=BURN, zorder=-1, lw=0)
        ax.grid(axis="y", color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)

for j, (_, title) in enumerate(COLS):
    axes[0, j].set_title(title, fontsize=10, color=INK, loc="left")
for ax in axes[-1]:
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[11, 1, 3]))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))

handles = [Line2D([], [], color=COLORS[k], lw=2, label=f"R_t = {k}") for k in COLORS]
handles += [Line2D([], [], color=INK2, lw=0.8, alpha=0.6, label="observed cases"),
            plt.Rectangle((0, 0), 1, 1, color=BURN, label="burn-in (R_t = 1.0)")]
fig.legend(handles=handles, loc="upper left", ncol=5, frameon=False, bbox_to_anchor=(0.015, 0.99))
fig.suptitle("Constant-R_t runs, no vaccination or waning (config_simple), mean of 5 iterations",
             x=0.02, y=1.003, ha="left", va="bottom", fontsize=12)
fig.tight_layout(rect=(0, 0, 1, 0.975))
fig.savefig(OUT, dpi=110, bbox_inches="tight", facecolor="#fcfcfb")
print(f"wrote {OUT}")
