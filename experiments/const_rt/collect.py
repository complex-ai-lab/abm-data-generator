"""Merge the constant-R_t runs into experiments/const_rt/all_trajectories.csv and check them.

Run from the repo root:  .venv/bin/python experiments/const_rt/collect.py
Checks per run: R_t is 1.0 during burn-in and the manifest value afterwards, no vaccination
(V == 0), S+E+I+R+V constant (= N), and config_used.yaml has WITH_VACC false / waning none.
"""
import os

import numpy as np
import pandas as pd
import yaml

MANIFEST = "experiments/const_rt/manifest.csv"
OUT = "experiments/const_rt/all_trajectories.csv"

man = pd.read_csv(MANIFEST, dtype={"fips": str})
frames, problems, burn_states = [], [], {}
for row in man.itertuples():
    d = f"results/{row.fips}/const_rt_{row.setting}"
    tag = f"{row.fips} {row.setting}"
    if not os.path.exists(f"{d}/seirv_daily.csv"):
        problems.append(f"{tag}: missing output in {d}")
        continue
    seirv = pd.read_csv(f"{d}/seirv_daily.csv")
    cases = pd.read_csv(f"{d}/cases_sim_vs_observed.csv")
    rt = pd.read_csv(f"{d}/r_t_daily.csv")
    cfg = yaml.safe_load(open(f"{d}/config_used.yaml"))

    burn = rt[rt["phase"] == "burn_in"]["r_t"].to_numpy()
    post = rt[rt["phase"] != "burn_in"]["r_t"].to_numpy()
    if len(burn) != 14 or not np.allclose(burn, 1.0):
        problems.append(f"{tag}: burn-in R_t not 14 x 1.0")
    if len(post) != 182 or not np.allclose(post, row.r_t, atol=1e-5):
        problems.append(f"{tag}: post-burn-in R_t != {row.r_t} (got {np.unique(post.round(6))[:5]})")
    if (seirv["V"] != 0).any():
        problems.append(f"{tag}: V is not always 0")
    tot = seirv[["S", "E", "I", "R", "V"]].sum(axis=1)
    if tot.max() - tot.min() > 1e-6 * tot.max():
        problems.append(f"{tag}: S+E+I+R+V not constant ({tot.min()}..{tot.max()})")
    if cfg["simulation_metadata"].get("WITH_VACC") is not False or \
            cfg["epidemic"].get("IMMUNITY_WANING_MODE") != "none":
        problems.append(f"{tag}: config_used.yaml is not the no-vaccine / no-waning config")

    burn_states[(row.fips, row.setting)] = seirv[seirv["day"] < 14][["S", "E", "I", "R"]].to_numpy()

    m = seirv.merge(cases.drop(columns=["date"]), on="day", how="inner")
    m.insert(0, "fips", row.fips)
    m.insert(1, "setting", row.setting)
    m.insert(2, "r_t", row.r_t)
    m.insert(3, "i0", row.i0)
    m.insert(4, "k", row.k)
    frames.append(m)

all_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
if frames:
    all_df.to_csv(OUT, index=False)
    print(f"wrote {len(all_df)} rows ({len(frames)} runs) to {OUT}")
    # Same county, same I0, burn-in R_t 1.0 and common random numbers: the 14 burn-in days
    # must be identical across the 3 settings (they differ in R_t only after burn-in).
    for fips in man["fips"].unique():
        got = [v for (f, _), v in burn_states.items() if f == fips]
        if len(got) > 1 and not all(np.array_equal(got[0], g) for g in got[1:]):
            problems.append(f"{fips}: burn-in S/E/I/R differ across settings")
print("\nproblems:" if problems else "\nall checks passed")
for p in problems:
    print("  " + p)
