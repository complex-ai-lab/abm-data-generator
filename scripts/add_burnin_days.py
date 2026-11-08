#!/usr/bin/env python
"""Prepend burn-in days to populations/pop<FIPS>/intervention.csv.

Why this exists. `daily_infected[t]` is the count of agents newly EXPOSED on
simulation day t, and simulation day 0 was the same calendar day as observation
day 0 (2020-10-26). But 2020-10-26 is roughly eight months into the pandemic:
the observed series already sits at 20-27% of its eventual peak on day one, with
a near-flat growth rate (implied early R of 0.76-1.23). The model, seeded with
at most `i0 = 0.0005` of the population -- about 22 agents -- into a ~100%
susceptible population, cannot produce that day-0 incidence. It needs a
prevalent infected pool roughly 11-14x larger than the `i0` box allows, so the
optimizer buys the level from R instead and drives week-0 R to the top of its
box (01031 saturated the spline coefficient box outright).

A burn-in fixes the cause rather than the symptom: the simulation starts
BURN_IN_DAYS before the observation window and is left unscored over that
stretch, so it builds its own prevalent infected pool -- and its own early
immunity -- before the first comparison. This is what the old 14-day loss
misalignment was doing accidentally, done deliberately and with the intervention
schedule shifted to match so that R(t) stays in phase with the NPIs and doses.

The prepended rows are unambiguous for these counties: school_intervention and
occ_intervention are both 0 on 2020-10-26 (no closure in force), and the vaccine
rollout does not begin until December, so the burn-in days carry 0 for all four
levers. Nothing is invented.

Usage:
    python scripts/add_burnin_days.py --days 14 17117 01031 36003
    python scripts/add_burnin_days.py --days 14 --revert 17117
"""
import argparse
import os
import sys

import pandas as pd

LEVER_COLS = ("school_intervention", "occ_intervention", "vaccines",
              "vaccines_65plus")


def marker_path(pop_dir):
    return os.path.join(pop_dir, "burnin_days.txt")


def current_burnin(pop_dir):
    p = marker_path(pop_dir)
    return int(open(p).read().strip()) if os.path.exists(p) else 0


def add_burnin(fips, days, repo_root, revert=False, carry_npi=False):
    pop_dir = os.path.join(repo_root, "populations", f"pop{fips}")
    iv_path = os.path.join(pop_dir, "intervention.csv")
    if not os.path.exists(iv_path):
        raise RuntimeError(f"no intervention.csv at {iv_path}")

    iv = pd.read_csv(iv_path)
    iv["date"] = pd.to_datetime(iv["date"]).dt.normalize()
    have = current_burnin(pop_dir)

    if revert:
        if have == 0:
            print(f"[{fips}] no burn-in recorded; nothing to revert")
            return
        iv = iv.iloc[have:].reset_index(drop=True)
        iv["t"] = range(len(iv))
        iv["date"] = iv["date"].dt.strftime("%Y-%m-%d")
        iv.to_csv(iv_path, index=False)
        os.remove(marker_path(pop_dir))
        print(f"[{fips}] reverted {have} burn-in days -> {len(iv)} rows, "
              f"starts {iv['date'].iloc[0]}")
        return

    if have == days:
        print(f"[{fips}] already has {days} burn-in days "
              f"({len(iv)} rows, starts {iv['date'].iloc[0].date()}); skipping")
        return
    if have:
        raise RuntimeError(
            f"[{fips}] already has {have} burn-in days, not {days}. Revert "
            f"first: python scripts/add_burnin_days.py --days {have} --revert {fips}")

    for c in LEVER_COLS:
        if c not in iv.columns:
            raise RuntimeError(
                f"[{fips}] intervention.csv has no {c!r} column; run "
                f"scripts/backfill_vaccines_65plus.py first")

    first = iv.iloc[0]
    # The burn-in must not invent policy. The NPI columns carry the first
    # observed severity backwards; when that is not the no-closure baseline
    # this is an assumption rather than a restatement, so it is refused unless
    # --carry-npi is given (decision 2026-09-17 for the 12 counties whose
    # occ_intervention is already 1 on 2020-10-26: 8 MI, 3 ND, 42075; in MI the
    # flag is 1 for the whole window, so the carried value is a constant).
    carried = {}
    for c in ("school_intervention", "occ_intervention"):
        if float(first[c]) != 0.0:
            if not carry_npi:
                raise RuntimeError(
                    f"[{fips}] {c} is {first[c]} on the first day "
                    f"({first['date'].date()}), not 0. Carrying a non-zero NPI "
                    f"severity into the burn-in would be an assumption; supply the "
                    f"real pre-window series or pass --carry-npi.")
            carried[c] = first[c]
    if float(first["vaccines"]) != 0.0:
        raise RuntimeError(
            f"[{fips}] vaccines is {first['vaccines']} on the first day; the "
            f"burn-in assumes the rollout had not started.")

    start = iv["date"].iloc[0]
    new_dates = pd.date_range(start - pd.Timedelta(days=days), periods=days,
                              freq="D")
    pre = pd.DataFrame({"date": new_dates})
    for c in iv.columns:
        if c == "date":
            continue
        pre[c] = carried.get(c, 0)
    pre = pre[iv.columns]

    out = pd.concat([pre, iv], ignore_index=True)
    out["t"] = range(len(out))
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    out.to_csv(iv_path, index=False)
    with open(marker_path(pop_dir), "w") as f:
        f.write(str(days))

    print(f"[{fips}] prepended {days} burn-in days: {len(iv)} -> {len(out)} rows, "
          f"{out['date'].iloc[0]} -> {out['date'].iloc[-1]}")
    if carried:
        print(f"[{fips}]   NPI carried back through the burn-in (not 0): "
              + ", ".join(f"{c}={v}" for c, v in carried.items()))
    print(f"[{fips}]   observation window now starts at t={days} "
          f"({out['date'].iloc[days]})")
    print(f"[{fips}]   dose totals unchanged: vaccines={out['vaccines'].sum():.0f}, "
          f"65+={out['vaccines_65plus'].sum():.0f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fips", nargs="+")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--carry-npi", action="store_true",
                    help="if an NPI column is non-zero on the first observed day, carry that "
                         "value back through the burn-in days instead of refusing")
    ap.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    a = ap.parse_args()
    rc = 0
    for f in a.fips:
        try:
            add_burnin(f, a.days, a.repo_root, revert=a.revert, carry_npi=a.carry_npi)
        except Exception as exc:                     # noqa: BLE001
            print(f"[{f}] FAILED: {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
