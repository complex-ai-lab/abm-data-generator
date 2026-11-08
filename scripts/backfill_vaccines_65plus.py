#!/usr/bin/env python
"""Backfill `vaccines_65plus` into an existing populations/pop<FIPS>/intervention.csv.

Implements Tasks/factual_vaccine_allocation.md section 3.1b.

The model's factual run allocated vaccines uniformly at random among
susceptibles, because `scripts/process_counties.py` only ever requested the
aggregate `administered_dose1_recip` column from CDC dataset 8xkx-amqh. That
dataset also carries `administered_dose1_recip_65plus`, which is what makes the
observed elderly-first rollout representable. This script adds that second
series to county folders that already exist, without re-running
`scripts/data_prep.sh` (which would rewrite all 154 population folders and hit
several other APIs).

Safety: the aggregate `vaccines` column is recomputed from the live API and
compared against the committed file. If CDC has revised the source since the
folder was built, the two will disagree and this script RAISES rather than
silently changing the factual dose count under existing calibrations.

Usage:
    python scripts/backfill_vaccines_65plus.py 17117 01031 36003
    python scripts/backfill_vaccines_65plus.py --check-only 17117
"""
import argparse
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

CDC_RESOURCE = "https://data.cdc.gov/resource/8xkx-amqh.csv"
COL_TOTAL = "administered_dose1_recip"
COL_65P = "administered_dose1_recip_65plus"

# Cumulative 65+ first-dose counts over the model window, from
# Tasks/factual_vaccine_allocation.md section 4.1. Used as an ingestion check.
EXPECTED_CUM_65P = {"17117": 7489, "01031": 5436, "36003": 4957}


def build_url(fips: str, start: str, end: str) -> str:
    """CDC series for one county over [start, end], inclusive, ordered by date."""
    where = (f"date between '{start}T00:00:00' and '{end}T00:00:00'")
    q = {
        "$select": f"date,fips,{COL_TOTAL},{COL_65P}",
        "$where": where,
        "$limit": "50000",
        "$order": "date",
    }
    return f"{CDC_RESOURCE}?fips={fips}&" + urllib.parse.urlencode(q)


def fetch(url: str, timeout: int = 120) -> pd.DataFrame:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    df = pd.read_csv(io.StringIO(raw))
    if df.empty:
        raise RuntimeError(f"backfill: CDC returned no rows for {url}")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    for c in (COL_TOTAL, COL_65P):
        if c not in df.columns:
            raise RuntimeError(f"backfill: CDC response missing column {c!r}")
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df.sort_values("date").reset_index(drop=True)


def daily_from_cumulative(cdc: pd.DataFrame) -> pd.DataFrame:
    """Difference the cumulative series into daily counts, with the clipping
    from section 3.1a.

    Cumulative series are monotone in this window for the three study counties,
    but CDC has revised counts downward elsewhere. Clip so a revision cannot
    produce a negative daily dose count, and so the 65+ stratum -- a subset --
    can never exceed the total it is a subset of.
    """
    out = cdc.copy()
    out["new_vax"] = out[COL_TOTAL].diff().fillna(0.0).clip(lower=0.0)
    out["new_vax_65plus"] = out[COL_65P].diff().fillna(0.0).clip(lower=0.0)
    out["new_vax_65plus"] = np.minimum(out["new_vax_65plus"], out["new_vax"])
    return out[["date", "new_vax", "new_vax_65plus"]]


def backfill_one(fips: str, repo_root: str, check_only: bool = False,
                 accept_revision: bool = False) -> dict:
    pop_dir = os.path.join(repo_root, "populations", f"pop{fips}")
    iv_path = os.path.join(pop_dir, "intervention.csv")
    if not os.path.exists(iv_path):
        raise RuntimeError(f"backfill: no intervention.csv at {iv_path}")

    iv = pd.read_csv(iv_path)
    if "vaccines" not in iv.columns:
        raise RuntimeError(f"backfill: {iv_path} has no 'vaccines' column")
    iv["date"] = pd.to_datetime(iv["date"]).dt.normalize()

    start = iv["date"].iloc[0]
    end = iv["date"].iloc[-1]
    # One extra day of lead-in so the first in-window day gets a real diff
    # rather than the fillna(0) that a series starting exactly at `start` gives.
    url = build_url(fips, (start - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    end.strftime("%Y-%m-%d"))
    cdc = fetch(url)
    daily = daily_from_cumulative(cdc)

    merged = iv[["date"]].merge(daily, on="date", how="left")
    new_vax = merged["new_vax"].fillna(0.0).values
    new_vax_65p = merged["new_vax_65plus"].fillna(0.0).values

    # --- the guard: has the source been revised under us? --------------------
    committed = iv["vaccines"].astype(float).values
    max_diff = float(np.abs(committed - new_vax).max())
    revision = None
    if max_diff > 1e-6 and accept_revision:
        n_rows = int((np.abs(committed - new_vax) > 1e-6).sum())
        revision = {"committed_cum_vaccines": float(committed.sum()),
                    "revised_cum_vaccines": float(new_vax.sum()),
                    "rows_changed": n_rows, "max_abs_diff": max_diff}
        print(f"[{fips}] --accept-revision: 'vaccines' replaced by the current CDC series: "
              f"{committed.sum():.0f} -> {new_vax.sum():.0f} doses, {n_rows} row(s) changed, "
              f"largest {max_diff:.0f}")
        iv["vaccines"] = new_vax
    elif max_diff > 1e-6:
        bad = int(np.argmax(np.abs(committed - new_vax)))
        raise RuntimeError(
            f"backfill {fips}: recomputed 'vaccines' disagrees with the committed "
            f"file by {max_diff:.6f} (worst at row {bad}, "
            f"{iv['date'].iloc[bad].date()}: committed {committed[bad]:.4f} vs "
            f"recomputed {new_vax[bad]:.4f}). CDC has revised the source since "
            f"this population folder was built. Refusing to change the factual "
            f"dose count silently -- resolve manually.")

    # --- ingestion checks from section 4.1 -----------------------------------
    if (new_vax_65p < 0).any() or (new_vax < 0).any():
        raise RuntimeError(f"backfill {fips}: negative daily dose count after clipping")
    if (new_vax_65p > new_vax + 1e-9).any():
        raise RuntimeError(f"backfill {fips}: vaccines_65plus exceeds vaccines on some row")
    frac = new_vax_65p - np.round(new_vax_65p)
    if np.abs(frac).max() > 1e-6:
        raise RuntimeError(
            f"backfill {fips}: vaccines_65plus is not integral "
            f"(max fractional part {np.abs(frac).max():.6g}); section 3.2 assumes "
            f"both bands are whole doses and does no apportionment rounding")

    cum_total = float(new_vax.sum())
    cum_65p = float(new_vax_65p.sum())
    expected = EXPECTED_CUM_65P.get(fips)
    exp_note = ""
    if expected is not None:
        exp_note = f"  (runbook expects {expected}, diff {cum_65p - expected:+.0f})"

    result = {
        "fips": fips,
        "rows": int(len(iv)),
        "date_start": str(start.date()),
        "date_end": str(end.date()),
        "cum_vaccines": cum_total,
        "cum_vaccines_65plus": cum_65p,
        "share_65plus": (cum_65p / cum_total) if cum_total else 0.0,
        "expected_cum_65plus": expected,
        "vaccines_max_abs_diff_vs_committed": max_diff,
        "vaccines_revision_accepted": revision,
        "query_url": url,
        "retrieved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    print(f"[{fips}] rows={len(iv)}  {start.date()} -> {end.date()}")
    if revision is None:
        print(f"[{fips}] vaccines matches committed file (max abs diff {max_diff:.2e})")
    print(f"[{fips}] cumulative: total={cum_total:.0f}  65+={cum_65p:.0f} "
          f"({100 * result['share_65plus']:.1f}%){exp_note}")

    if check_only:
        print(f"[{fips}] --check-only: not writing")
        return result

    if "vaccines_65plus" in iv.columns:
        iv = iv.drop(columns=["vaccines_65plus"])
    # Insert directly after `vaccines` so the file stays readable; `vaccines`
    # keeps its own name, position and meaning so nothing that reads it changes.
    cols = list(iv.columns)
    pos = cols.index("vaccines") + 1
    iv.insert(pos, "vaccines_65plus", new_vax_65p)
    iv["date"] = iv["date"].dt.strftime("%Y-%m-%d")
    iv.to_csv(iv_path, index=False)
    print(f"[{fips}] wrote {iv_path} with vaccines_65plus at column {pos}")

    prov = os.path.join(pop_dir, "vaccines_65plus_provenance.json")
    with open(prov, "w") as f:
        json.dump(result, f, indent=2)
    print(f"[{fips}] wrote {prov}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fips", nargs="+", help="county FIPS codes, e.g. 17117 01031 36003")
    ap.add_argument("--check-only", action="store_true",
                    help="fetch and validate but do not rewrite intervention.csv")
    ap.add_argument("--accept-revision", action="store_true",
                    help="if CDC has revised the series, overwrite the committed 'vaccines' "
                         "column with the current one (recorded in the provenance json). "
                         "Only for counties with no calibration yet.")
    ap.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()

    failures = []
    for fips in args.fips:
        try:
            backfill_one(fips, args.repo_root, check_only=args.check_only,
                         accept_revision=args.accept_revision)
        except Exception as exc:            # noqa: BLE001 - report and continue
            print(f"[{fips}] FAILED: {exc}", file=sys.stderr)
            failures.append((fips, str(exc)))
        print()

    if failures:
        print(f"{len(failures)} county/counties failed:", file=sys.stderr)
        for fips, msg in failures:
            print(f"  {fips}: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
