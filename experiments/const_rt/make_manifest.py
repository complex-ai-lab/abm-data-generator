"""Build the parameter manifest for the constant-R_t, no-vaccine runs.

For each county, from its simple-model calibration (reference_results_simple/<FIPS>/config_01031.yaml;
every file there has that name, but each folder's parameters belong to its own county):
  - keep the weekly R_t values within [5th, 95th] percentile, take their mean mu and sample sd sigma
  - 3 settings: mean (R_t = mu), draw1 and draw2 (one draw each from N(mu, sigma), redrawn until
    within mu +/- sigma and within [R_MIN, R_MAX], the calibration bounds R_T_MIN / R_T_MAX); the
    draws are redrawn together until mean, draw1 and draw2 are pairwise >= MIN_GAP * sigma apart
  - I0 and K are the county's own calibrated values

Run from the repo root:  .venv/bin/python experiments/const_rt/make_manifest.py
"""
import os

import numpy as np
import pandas as pd
import yaml

COUNTIES = "01031 13013 17037 19027 21071 27005 30029 36051 42059 47005 51149".split()
SRC = "reference_results_simple/{fips}/config_01031.yaml"
OUT = "experiments/const_rt/manifest.csv"
DRAW_SEED = 20261107
R_MIN, R_MAX = 0.1, 5.0
N_DRAWS = 2
MIN_GAP = 0.25          # the 3 R_t values of a county must be pairwise >= MIN_GAP * sigma apart

# Each county has its own generator, seeded by (DRAW_SEED, fips, stream), so redrawing one county
# does not change the others. Bump a county's stream here to redraw it.
STREAM = {"17037": 1}   # 17037: stream 0 gave two nearly identical draws (1.0663 / 1.0663)

rows = []
for fips in COUNTIES:
    stream = STREAM.get(fips, 0)
    rng = np.random.default_rng([DRAW_SEED, int(fips), stream])
    epi = yaml.safe_load(open(SRC.format(fips=fips)))["epidemic"]
    r = np.asarray(epi["R_T"], dtype=float)
    p5, p95 = np.percentile(r, [5, 95])
    kept = r[(r >= p5) & (r <= p95)]
    mu, sigma = kept.mean(), kept.std(ddof=1)
    common = dict(fips=fips, mu=round(mu, 6), sigma=round(sigma, 6), n_kept=len(kept),
                  p5=round(p5, 6), p95=round(p95, 6), i0=float(epi["INITIAL_INFECTED"]),
                  k=float(epi["K"]), draw_seed=f"{DRAW_SEED}-{fips}-{stream}")
    rows.append(dict(setting="mean", r_t=round(mu, 6), **common))

    def draw_one():
        # redraw until within one sd of the mean (and within the calibration bounds)
        while True:
            x = rng.normal(mu, sigma)
            if abs(x - mu) <= sigma and R_MIN <= x <= R_MAX:
                return x

    # redraw the whole set until the 3 values (mean + draws) are pairwise >= MIN_GAP * sigma apart
    while True:
        draws = [draw_one() for _ in range(N_DRAWS)]
        vals = [mu] + draws
        if all(abs(a - b) >= MIN_GAP * sigma for i, a in enumerate(vals) for b in vals[i + 1:]):
            break
    for j, x in enumerate(draws, start=1):
        rows.append(dict(setting=f"draw{j}", r_t=round(x, 6), **common))

cols = ["fips", "setting", "r_t", "mu", "sigma", "n_kept", "p5", "p95", "i0", "k", "draw_seed"]
df = pd.DataFrame(rows)[cols]
os.makedirs(os.path.dirname(OUT), exist_ok=True)
df.to_csv(OUT, index=False)
print(df.to_string(index=False))
print(f"\nwrote {len(df)} rows to {OUT}")
