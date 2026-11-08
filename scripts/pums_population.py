#!/usr/bin/env python3
"""P0 Tier 1 (thoughts/Nature_Collection_Positioning.md 6.1 items 1-3, 6):
whole-household synthetic population from ACS PUMS, raked to county marginals.

For one county:
  1. PUMA(s) covering the county from the 2010 tract->PUMA relationship file.
  2. ACS 2020 5-year PUMS person records for those PUMA(s) (Census API), cached
     under data/pums/.  Housing-unit persons are grouped into whole households
     by SERIALNO; group-quarters (GQ) persons are kept as GQ persons.
  3. Iterative proportional updating (IPU, Ye et al. 2009) of the household
     weights so the person-level totals match the county marginals:
        age (9 bins, B01001 via data/state_data/<ST>/<FIPS>/agents_ages.csv)
        persons by household size (1..6+, B11016 via agents_household_sizes.csv)
        employed persons by sector (19 sectors, ACS C24030 via
        data/state_data/<ST>/<FIPS>/agents_occupations_c24030.csv)
     scaled to the target agent count (default: the agent count of the
     population being replaced, so num_agents / dose series / k are unchanged).
  4. Integer synthesis: households sampled with probability proportional to the
     raked weight until the housing-unit target is reached; GQ persons sampled
     by person weight and grouped into pseudo-households of at most GQ_CAP
     members within GQ type (pre-registered cap, 10 -- the engine's household
     sanity check rejects anything larger).
  5. Employment from PUMS ESR: employed (1,2) and armed forces (4,5) carry a
     sector from INDP; unemployed (3) -> "Unemployed"; not in labour force /
     under 16 -> blank.  Nobody is put in the labour force by age alone.
  6. Writes the population csv in the committed schema
     (ID, Age, Household Size, Occupations, Household ID) plus provenance
     columns, regenerates populations/pop<FIPS>/{age.csv,age.pickle,
     disease_stages.csv}, archives the replaced files under
     data/population_data_pre_P0/ and populations/pop<FIPS>/_pre_P0/, and
     writes a validation exhibit (csv + png) comparing old / new / target.

Usage: python scripts/pums_population.py <FIPS> --out-dir <dir> [--target-n N]
       [--seed 20260915] [--gq-cap 10] [--no-swap]
"""
import argparse
import json
import os
import pickle
import shutil
import sys
from collections import defaultdict

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(REPO, ".env"))
PUMS_DIR = os.path.join(REPO, "data", "pums")
os.makedirs(PUMS_DIR, exist_ok=True)

FIPS_TO_ST = {
    '01': 'AL', '13': 'GA', '17': 'IL', '19': 'IA', '20': 'KS', '21': 'KY', '22': 'LA',
    '26': 'MI', '27': 'MN', '29': 'MO', '30': 'MT', '36': 'NY', '38': 'ND', '39': 'OH',
    '40': 'OK', '42': 'PA', '46': 'SD', '47': 'TN', '48': 'TX', '51': 'VA',
}
PUMS_YEAR = 2020
PUMS_VARS = ["SERIALNO", "SPORDER", "AGEP", "SEX", "ESR", "INDP", "SCH",
             "WGTP", "PWGTP", "NP", "TYPEHUGQ", "RELSHIPP"]

SECTOR_ORDER = [
    "AGRICULTURE", "MINING", "UTILITIES", "CONSTRUCTION", "MANUFACTURING",
    "WHOLESALETRADE", "RETAILTRADE", "TRANSPORTATION", "INFORMATION",
    "FINANCEINSURANCE", "REALESTATERENTAL", "SCIENTIFICTECHNICAL",
    "ENTERPRISEMANAGEMENT", "WASTEMANAGEMENT", "EDUCATION", "HEALTHCARE",
    "ART", "FOOD", "OTHER",
]
# 2017 census industry codes (PUMS INDP) -> repo sector. Ranges are the
# published code blocks; the label prefix in the API's INDP.json is checked
# against each range in indp_sector() so a wrong block fails loud.
INDP_RANGES = [
    (170, 290, "AGR", "AGRICULTURE"),
    (370, 490, "EXT", "MINING"),
    (570, 690, "UTL", "UTILITIES"),
    (770, 770, "CON", "CONSTRUCTION"),
    (1070, 3990, "MFG", "MANUFACTURING"),
    (4070, 4590, "WHL", "WHOLESALETRADE"),
    (4670, 5790, "RET", "RETAILTRADE"),
    (6070, 6390, "TRN", "TRANSPORTATION"),
    (6470, 6780, "INF", "INFORMATION"),
    (6870, 6992, "FIN", "FINANCEINSURANCE"),
    (7071, 7190, "FIN", "REALESTATERENTAL"),
    (7270, 7490, "PRF", "SCIENTIFICTECHNICAL"),
    (7570, 7570, "PRF", "ENTERPRISEMANAGEMENT"),
    (7580, 7790, "PRF", "WASTEMANAGEMENT"),
    (7860, 7890, "EDU", "EDUCATION"),
    (7970, 8290, "MED", "HEALTHCARE"),
    (8370, 8470, "SCA", "HEALTHCARE"),      # social assistance: C24030 "health care and social assistance"
    (8560, 8590, "ENT", "ART"),
    (8660, 8690, "ENT", "FOOD"),
    (8770, 9290, "SRV", "OTHER"),
    (9370, 9590, "ADM", "OTHER"),
    (9670, 9870, "MIL", "OTHER"),
]
AGE_BINS = [(0, 9), (10, 19), (20, 29), (30, 39), (40, 49), (50, 59), (60, 69), (70, 79), (80, 200)]
AGE_LABELS = ["AGE_0_9", "AGE_10_19", "AGE_20_29", "AGE_30_39", "AGE_40_49",
              "AGE_50_59", "AGE_60_69", "AGE_70_79", "AGE_80"]


def die(msg):
    print(f"[pums_population] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- fetch ----
def pumas_for_county(fips):
    path = os.path.join(PUMS_DIR, "2010_Census_Tract_to_2010_PUMA.txt")
    if not os.path.exists(path):
        r = requests.get("https://www2.census.gov/geo/docs/maps-data/data/rel/"
                         "2010_Census_Tract_to_2010_PUMA.txt", timeout=120)
        r.raise_for_status()
        open(path, "w").write(r.text)
    xw = pd.read_csv(path, dtype=str)
    sel = xw[(xw.STATEFP == fips[:2]) & (xw.COUNTYFP == fips[2:])]
    pumas = sorted(sel.PUMA5CE.unique())
    if not pumas:
        die(f"no PUMA found for {fips}")
    return pumas


def check_pums(df, fips, where):
    """Fail loud on a corrupt download instead of caching it. The 39023 and
    51095 caches (2026-09-16) held rows with glued-together values (AGEP 4074,
    WGTP 2525, SPORDER 1002) from one bad API response; a clean re-fetch of the
    same PUMA returned the right values (AGEP 74)."""
    a = pd.to_numeric(df["AGEP"], errors="coerce")
    s = pd.to_numeric(df["SEX"], errors="coerce")
    t = pd.to_numeric(df["TYPEHUGQ"], errors="coerce")
    bad = a.isna() | (a > 99) | ~s.isin([1, 2]) | ~t.isin([1, 2, 3])
    if bad.any():
        die(f"{fips}: {int(bad.sum())} corrupt PUMS person rows in {where} "
            f"(e.g. AGEP={df['AGEP'][bad].iloc[0]!r}); delete the cache and re-fetch")


def fetch_pums(fips):
    cache = os.path.join(PUMS_DIR, f"{fips}_pums{PUMS_YEAR}_persons.csv")
    if os.path.exists(cache):
        df = pd.read_csv(cache, dtype=str)
        check_pums(df, fips, cache)
        return df
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        die("CENSUS_API_KEY missing from .env")
    frames = []
    for puma in pumas_for_county(fips):
        url = (f"https://api.census.gov/data/{PUMS_YEAR}/acs/acs5/pums?get={','.join(PUMS_VARS)}"
               f"&for=public%20use%20microdata%20area:{puma}&in=state:{fips[:2]}&key={key}")
        r = requests.get(url, timeout=300)
        if r.status_code != 200:
            die(f"PUMS fetch {fips} PUMA {puma}: HTTP {r.status_code} {r.text[:200]}")
        js = r.json()
        df = pd.DataFrame(js[1:], columns=js[0])
        df["PUMA"] = puma
        frames.append(df)
        print(f"[pums_population] {fips}: PUMA {puma} -> {len(df)} person rows")
    out = pd.concat(frames, ignore_index=True)
    check_pums(out, fips, "API response")
    out.to_csv(cache, index=False)
    return out


def indp_labels():
    path = os.path.join(PUMS_DIR, f"INDP_{PUMS_YEAR}.json")
    if not os.path.exists(path):
        r = requests.get(f"https://api.census.gov/data/{PUMS_YEAR}/acs/acs5/pums/variables/INDP.json",
                         timeout=120)
        r.raise_for_status()
        open(path, "w").write(r.text)
    return json.load(open(path))["values"]["item"]


def indp_sector(code, labels):
    """repo sector for one INDP code, or None (not applicable)."""
    if code is None or code == "" or int(code) <= 0:
        return None
    c = int(code)
    lab = labels.get(str(c), labels.get(f"{c:04d}", ""))
    for lo, hi, prefix, sector in INDP_RANGES:
        if lo <= c <= hi:
            if lab and not lab.startswith(prefix):
                die(f"INDP {c} label {lab!r} does not match block prefix {prefix}")
            return sector
    if lab.startswith("N/A") or lab.startswith("Unemployed"):
        return None
    die(f"INDP code {c} ({lab!r}) not covered by INDP_RANGES")


# ------------------------------------------------------------ marginals ----
def county_marginals(fips, st):
    d = os.path.join(REPO, "data", "state_data", st, fips)
    ages = pd.read_csv(os.path.join(d, "agents_ages.csv"))
    hh = pd.read_csv(os.path.join(d, "agents_household_sizes.csv"))
    occ_p = os.path.join(d, "agents_occupations_c24030.csv")
    if not os.path.exists(occ_p):
        die(f"C24030 sector file missing: {occ_p} (run scripts/fetch_industry_c24030.py)")
    occ = pd.read_csv(occ_p)
    age_t = dict(zip(ages.Age, ages.Number.astype(float)))
    if list(age_t) != AGE_LABELS:
        die(f"unexpected age labels {list(age_t)}")
    hh_persons_t = {int(r["Household Size"]): float(r["Household Size"]) * float(r["Number"])
                    for _, r in hh.iterrows()}          # persons living in households of each size
    sector_t = dict(zip(occ.Occupation, occ.Number.astype(float)))
    if set(sector_t) != set(SECTOR_ORDER):
        die(f"sector file columns {set(sector_t)} != {set(SECTOR_ORDER)}")
    return age_t, hh_persons_t, sector_t


def age_bin(a):
    for i, (lo, hi) in enumerate(AGE_BINS):
        if lo <= a <= hi:
            return AGE_LABELS[i]
    die(f"age {a} out of range")


# ------------------------------------------------------------------ IPU ----
def ipu(H, targets, n_iter=400, tol=1e-4):
    """H: [n_hh, n_cat] person counts per household per category (float);
    targets: [n_cat]. Returns raked household weights (starts from PUMS WGTP)."""
    w = H["w0"].copy()
    M = H["M"]
    nz = [np.flatnonzero(M[:, c]) for c in range(M.shape[1])]
    hist = []
    for it in range(n_iter):
        for c in range(M.shape[1]):
            idx = nz[c]
            if idx.size == 0 or targets[c] <= 0:
                continue
            cur = float(M[idx, c] @ w[idx])
            if cur > 0:
                w[idx] *= targets[c] / cur
        tot = M.T @ w
        rel = np.abs(tot - targets) / np.maximum(targets, 1.0)
        hist.append(float(rel[targets > 0].mean()))
        if hist[-1] < tol:
            break
    return w, hist


# ---------------------------------------------------------------- build ----
def build(fips, target_n, seed, gq_cap, out_dir):
    st = FIPS_TO_ST.get(fips[:2])
    if st is None:
        die(f"state abbreviation unknown for {fips}")
    rng = np.random.default_rng(seed)
    labels = indp_labels()
    per = fetch_pums(fips)
    for c in ["AGEP", "SEX", "ESR", "INDP", "SCH", "WGTP", "PWGTP", "NP", "TYPEHUGQ", "RELSHIPP", "SPORDER"]:
        per[c] = pd.to_numeric(per[c], errors="coerce")
    per["ESR"] = per["ESR"].fillna(0).astype(int)      # blank = under 16
    per["INDP"] = per["INDP"].fillna(0).astype(int)
    per["SCH"] = per["SCH"].fillna(0).astype(int)
    per["AGEP"] = per["AGEP"].astype(int)
    per["age_bin"] = per["AGEP"].map(age_bin)
    per["sector"] = [indp_sector(i, labels) if e in (1, 2, 4, 5) else None
                     for i, e in zip(per["INDP"], per["ESR"])]
    per["Occupations"] = [s if isinstance(s, str) else ("Unemployed" if e == 3 else None)
                          for s, e in zip(per["sector"], per["ESR"])]

    age_t, hh_persons_t, sector_t = county_marginals(fips, st)
    total_pop_census = sum(age_t.values())

    hu = per[per.TYPEHUGQ == 1].copy()
    gq = per[per.TYPEHUGQ.isin([2, 3])].copy()
    # the engine's household loader rejects households of more than 10 members
    # (transition.py _require_household_members sanity check); PUMS housing
    # units above that size are rare and are dropped from the sampling pool.
    big_serials = hu.groupby("SERIALNO").size()
    big_serials = set(big_serials[big_serials > 10].index)
    if big_serials:
        print(f"[pums_population] {fips}: dropping {len(big_serials)} PUMS households with > 10 members "
              f"({int(hu.SERIALNO.isin(big_serials).sum())} person rows)")
        hu = hu[~hu.SERIALNO.isin(big_serials)]
    if hu.WGTP.isna().any() or (hu.WGTP <= 0).any():
        hu = hu[hu.WGTP > 0]
    gq_share = float(gq.PWGTP.sum()) / float(gq.PWGTP.sum() + hu.PWGTP.sum())
    n_gq = int(round(target_n * gq_share))
    n_hu = target_n - n_gq
    print(f"[pums_population] {fips}: PUMS HU persons {len(hu)}, GQ persons {len(gq)} "
          f"(GQ weight share {gq_share:.3%}); target N={target_n} -> HU {n_hu}, GQ {n_gq}")

    # ---- household matrix for IPU --------------------------------------
    cats = ([f"age:{a}" for a in AGE_LABELS] +
            [f"hh:{s}" for s in range(1, 7)] +
            [f"sec:{s}" for s in SECTOR_ORDER])
    cidx = {c: i for i, c in enumerate(cats)}
    hh_groups = hu.groupby("SERIALNO", sort=True)
    serials = list(hh_groups.groups.keys())
    n_hh = len(serials)
    M = np.zeros((n_hh, len(cats)), dtype=np.float64)
    w0 = np.zeros(n_hh)
    sizes = np.zeros(n_hh, dtype=int)
    members = []
    for k, (sid, g) in enumerate(hh_groups):
        sz = len(g)
        sizes[k] = sz
        w0[k] = float(g.WGTP.iloc[0])
        members.append(g.index.to_numpy())
        for a in g.age_bin:
            M[k, cidx[f"age:{a}"]] += 1
        M[k, cidx[f"hh:{min(sz, 6)}"]] += sz
        for s in g.sector:
            if isinstance(s, str):
                M[k, cidx[f"sec:{s}"]] += 1

    # ---- targets scaled to the housing-unit agent count -------------------
    scale_age = n_hu / total_pop_census
    hh_persons_total = sum(hh_persons_t.values())
    scale_hh = n_hu / hh_persons_total
    # employed persons (C24030) scaled like population; GQ persons are mostly
    # outside the labour force so scale by the HU share as well
    scale_sec = n_hu / total_pop_census
    targets = np.zeros(len(cats))
    for a in AGE_LABELS:
        targets[cidx[f"age:{a}"]] = age_t[a] * scale_age
    for s in range(1, 7):
        targets[cidx[f"hh:{s}"]] = hh_persons_t[s] * scale_hh
    for s in SECTOR_ORDER:
        targets[cidx[f"sec:{s}"]] = sector_t[s] * scale_sec

    w, hist = ipu({"w0": w0, "M": M}, targets)
    fit = M.T @ w
    print(f"[pums_population] IPU: {len(hist)} sweeps, mean rel. error {hist[-1]:.4f}")

    # ---- integer synthesis: sample whole households -----------------------
    p = w / w.sum()
    persons_per_draw = float(sizes @ p)
    n_draw = int(n_hu / persons_per_draw * 1.05) + 50
    order = rng.choice(n_hh, size=n_draw, replace=True, p=p)
    cum = np.cumsum(sizes[order])
    k_stop = int(np.searchsorted(cum, n_hu, side="left"))
    chosen = order[:k_stop + 1]
    # trim to exactly n_hu by dropping the last household(s) if the overshoot
    # is larger than the undershoot
    while chosen.size and sizes[chosen].sum() > n_hu and \
            sizes[chosen].sum() - n_hu > n_hu - sizes[chosen[:-1]].sum():
        chosen = chosen[:-1]
    # exact agent count: the population size is a contract for num_agents,
    # disease_stages.csv and every downstream consumer, so pad by drawing one
    # more household and truncating its last members, or trim the last
    # household's last members. Only the final household can be partial.
    # Keep appending households until the count is reached. (Appending a single
    # household left the total short when that household was smaller than the
    # deficit: 46035 and 48013 came out 1 agent under target.)
    nxt = k_stop + 1
    while int(sizes[chosen].sum()) < n_hu:
        if nxt >= order.size:
            order = np.concatenate([order, rng.choice(n_hh, size=50, replace=True, p=p)])
        chosen = np.concatenate([chosen, order[nxt:nxt + 1]])
        nxt += 1
    rows = []
    hh_id = 0
    remaining = n_hu
    for h in chosen:
        if remaining <= 0:
            break
        hh_id += 1
        g = hu.loc[members[h]].iloc[:remaining]
        remaining -= len(g)
        for _, r in g.iterrows():
            rows.append(dict(Age=int(r.AGEP), Occupations=r.Occupations, HouseholdID=hh_id,
                             Sex=int(r.SEX), ESR=int(r.ESR), SCH=int(r.SCH), GQ=0,
                             PUMS_SERIALNO=r.SERIALNO, PUMS_SPORDER=int(r.SPORDER)))
    # ---- GQ persons -> capped pseudo-households --------------------------
    if n_gq > 0 and len(gq):
        pg = gq.PWGTP.to_numpy(float)
        pg = pg / pg.sum()
        pick = rng.choice(len(gq), size=n_gq, replace=True, p=pg)
        gq_pick = gq.iloc[pick]
        for gtype in (2, 3):
            sub = gq_pick[gq_pick.TYPEHUGQ == gtype]
            idx = rng.permutation(len(sub))
            for start in range(0, len(sub), gq_cap):
                hh_id += 1
                for j in idx[start:start + gq_cap]:
                    r = sub.iloc[j]
                    rows.append(dict(Age=int(r.AGEP), Occupations=r.Occupations, HouseholdID=hh_id,
                                     Sex=int(r.SEX), ESR=int(r.ESR), SCH=int(r.SCH), GQ=int(gtype),
                                     PUMS_SERIALNO=r.SERIALNO, PUMS_SPORDER=int(r.SPORDER)))
    pop = pd.DataFrame(rows)
    pop = pop.sample(frac=1.0, random_state=seed).reset_index(drop=True)   # shuffle agent order
    pop.insert(0, "ID", np.arange(len(pop)))
    hsize = pop.groupby("HouseholdID")["ID"].transform("size").astype(int)
    pop.insert(2, "Household Size", hsize)
    pop = pop.rename(columns={"HouseholdID": "Household ID"})
    pop = pop[["ID", "Age", "Household Size", "Occupations", "Household ID",
               "Sex", "ESR", "SCH", "GQ", "PUMS_SERIALNO", "PUMS_SPORDER"]]
    if pop["Household Size"].max() > 10:
        die("household larger than 10 after synthesis")
    print(f"[pums_population] synthesised {len(pop)} agents in {pop['Household ID'].nunique()} "
          f"households (target {target_n}, diff {len(pop) - target_n:+d})")
    if len(pop) != target_n:
        die(f"synthesised {len(pop)} agents != target {target_n}")
    return pop, dict(cats=cats, targets=targets, fit=fit, ipu_hist=hist, n_gq=n_gq, n_hu=n_hu,
                     gq_share=gq_share, total_pop_census=total_pop_census, st=st,
                     pumas=pumas_for_county(fips), n_pums_hh=n_hh, n_pums_gq=len(gq))


# ----------------------------------------------------------- validation ----
def summarise(pop, label):
    """Distributions used in the validation exhibit, for one population csv."""
    a = pop["Age"].to_numpy()
    hh = pop["Household ID"].to_numpy()
    out = {}
    out["n_agents"] = len(pop)
    for lab, (lo, hi) in zip(AGE_LABELS, AGE_BINS):
        out[f"age:{lab}"] = float(((a >= lo) & (a <= hi)).mean())
    size = pop.groupby("Household ID")["ID"].transform("size").to_numpy()
    # B11016 counts housing-unit households only, so group-quarters agents
    # (GQ > 0 in the P0 csv) are excluded from the household-size shares.
    hu_mask = (pop["GQ"].to_numpy() == 0) if "GQ" in pop.columns else np.ones(len(pop), bool)
    for s in range(1, 7):
        sel = (size == s) if s < 6 else (size >= 6)
        out[f"hh:{s}"] = float(sel[hu_mask].mean())
    out["gq_agent_share"] = float((~hu_mask).mean())
    occ = pop["Occupations"].fillna("")
    employed = occ.isin(SECTOR_ORDER)
    out["labour_force_share_all"] = float(employed.mean())
    for s in SECTOR_ORDER:
        out[f"sec:{s}"] = float((occ == s).sum()) / max(int(employed.sum()), 1)
    for lo, hi in [(16, 24), (25, 54), (55, 64), (65, 200)]:
        m = (a >= lo) & (a <= hi)
        out[f"lfp:{lo}-{hi if hi < 200 else 'plus'}"] = float(employed[m].mean()) if m.any() else np.nan
    df = pd.DataFrame({"hh": hh, "child": a < 18, "adult": a >= 18, "elder": a >= 65})
    g = df.groupby("hh").agg(n=("hh", "size"), n_child=("child", "sum"), n_adult=("adult", "sum"),
                             n_elder=("elder", "sum"))
    child_hh = g[g.n_child > 0]
    out["children_no_adult_share"] = float((child_hh.n_child[child_hh.n_adult == 0].sum()) / max(g.n_child.sum(), 1))
    elders = g.n_elder.sum()
    out["elderly_alone_share"] = float(g.n_elder[(g.n == 1) & (g.n_elder == 1)].sum() / max(elders, 1))
    out["mean_hh_size"] = float(g.n.mean())
    out["label"] = label
    return out


def validation_exhibit(fips, pop_new, pop_old, meta, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    s_new = summarise(pop_new, "P0 (PUMS)")
    s_old = summarise(pop_old, "old") if pop_old is not None else None
    tgt = {}
    T = dict(zip(meta["cats"], meta["targets"]))
    age_tot = sum(T[f"age:{a}"] for a in AGE_LABELS)
    for a in AGE_LABELS:
        tgt[f"age:{a}"] = T[f"age:{a}"] / age_tot
    hh_tot = sum(T[f"hh:{s}"] for s in range(1, 7))
    for s in range(1, 7):
        tgt[f"hh:{s}"] = T[f"hh:{s}"] / hh_tot
    sec_tot = sum(T[f"sec:{s}"] for s in SECTOR_ORDER)
    for s in SECTOR_ORDER:
        tgt[f"sec:{s}"] = T[f"sec:{s}"] / sec_tot
    tgt["labour_force_share_all"] = sec_tot / meta["n_hu"]
    tgt["label"] = "census target"

    rows = []
    keys = [k for k in s_new if k != "label"]
    for k in keys:
        rows.append(dict(metric=k, target=tgt.get(k, np.nan),
                         old=(s_old.get(k, np.nan) if s_old else np.nan), new=s_new.get(k, np.nan)))
    vdf = pd.DataFrame(rows)
    vdf.to_csv(os.path.join(out_dir, "population_validation.csv"), index=False)

    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    def bars(ax, prefix, labels, title):
        x = np.arange(len(labels)); wd = 0.27
        ax.bar(x - wd, [tgt.get(f"{prefix}{l}", np.nan) for l in labels], wd, label="census target", color="#444")
        if s_old:
            ax.bar(x, [s_old.get(f"{prefix}{l}", np.nan) for l in labels], wd, label="old population", color="#d62728")
        ax.bar(x + wd, [s_new.get(f"{prefix}{l}", np.nan) for l in labels], wd, label="P0 population", color="#1f77b4")
        ax.set_xticks(x); ax.set_xticklabels(labels, rotation=60, ha="right", fontsize=7)
        ax.set_title(title); ax.grid(alpha=0.3, axis="y")
    bars(axes[0, 0], "age:", AGE_LABELS, "age share")
    bars(axes[0, 1], "hh:", [str(s) for s in range(1, 7)], "share of agents by household size")
    bars(axes[1, 0], "sec:", SECTOR_ORDER, "sector share of the employed")
    ax = axes[1, 1]
    names = ["labour_force_share_all", "lfp:16-24", "lfp:25-54", "lfp:55-64", "lfp:65-plus",
             "children_no_adult_share", "elderly_alone_share"]
    x = np.arange(len(names)); wd = 0.27
    ax.bar(x - wd, [tgt.get(n, np.nan) for n in names], wd, color="#444", label="census target")
    if s_old:
        ax.bar(x, [s_old.get(n, np.nan) for n in names], wd, color="#d62728", label="old population")
    ax.bar(x + wd, [s_new.get(n, np.nan) for n in names], wd, color="#1f77b4", label="P0 population")
    ax.set_xticks(x); ax.set_xticklabels(names, rotation=60, ha="right", fontsize=7)
    ax.set_title("employment and household structure"); ax.grid(alpha=0.3, axis="y")
    axes[0, 0].legend(fontsize=8)
    fig.suptitle(f"{fips} synthetic population validation")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "population_validation.png"), dpi=150)
    plt.close(fig)
    return vdf


def regenerate_pop_folder(fips, pop):
    d = os.path.join(REPO, "populations", f"pop{fips}")
    bak = os.path.join(d, "_pre_P0")
    os.makedirs(bak, exist_ok=True)
    for f in ("age.csv", "age.pickle", "disease_stages.csv"):
        src = os.path.join(d, f)
        if os.path.exists(src) and not os.path.exists(os.path.join(bak, f)):
            shutil.copy2(src, os.path.join(bak, f))
    pop["Age"].to_csv(os.path.join(d, "age.csv"), index=False, header=["age"])
    codes = []
    for age in pop["Age"]:
        if age < 20: codes.append(5)
        elif age < 30: codes.append(0)
        elif age < 40: codes.append(1)
        elif age < 50: codes.append(2)
        elif age < 65: codes.append(3)
        else: codes.append(4)
    with open(os.path.join(d, "age.pickle"), "wb") as f:
        pickle.dump(pd.Series(codes, name="age"), f)
    pd.DataFrame([0.0] * len(pop), columns=["stages"]).to_csv(
        os.path.join(d, "disease_stages.csv"), index=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fips")
    ap.add_argument("--out-dir", required=True, help="where the validation exhibit goes")
    ap.add_argument("--target-n", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260915)
    ap.add_argument("--gq-cap", type=int, default=10)
    ap.add_argument("--no-swap", action="store_true",
                    help="write the new csv next to the exhibit only; do not replace the committed population")
    a = ap.parse_args()
    fips = a.fips
    st = FIPS_TO_ST[fips[:2]]
    os.makedirs(a.out_dir, exist_ok=True)

    old_p = os.path.join(REPO, "data", "population_data", f"{st}_population_data", f"{fips}_population.csv")
    arch_dir = os.path.join(REPO, "data", "population_data_pre_P0", f"{st}_population_data")
    arch_p = os.path.join(arch_dir, f"{fips}_population.csv")
    pop_old = None
    if os.path.exists(arch_p):
        pop_old = pd.read_csv(arch_p)
    elif os.path.exists(old_p):
        pop_old = pd.read_csv(old_p)
    target_n = a.target_n or (len(pop_old) if pop_old is not None else None)
    if target_n is None:
        # new county: one agent per resident (ACS B01001 total)
        ages_p = os.path.join(REPO, "data", "state_data", st, fips, "agents_ages.csv")
        target_n = int(round(pd.read_csv(ages_p).Number.sum()))
        print(f"[pums_population] no existing population; target_n = ACS total {target_n}")

    pop, meta = build(fips, target_n, a.seed, a.gq_cap, a.out_dir)
    vdf = validation_exhibit(fips, pop, pop_old, meta, a.out_dir)
    pop.to_csv(os.path.join(a.out_dir, f"{fips}_population_P0.csv"), index=False)
    with open(os.path.join(a.out_dir, "population_build_meta.json"), "w") as f:
        json.dump(dict(fips=fips, pums_year=PUMS_YEAR, pumas=meta["pumas"], seed=a.seed, gq_cap=a.gq_cap,
                       target_n=target_n, n_agents=len(pop), n_households=int(pop["Household ID"].nunique()),
                       n_hu_agents=int((pop.GQ == 0).sum()), n_gq_agents=int((pop.GQ > 0).sum()),
                       gq_weight_share_pums=meta["gq_share"], n_pums_households=meta["n_pums_hh"],
                       n_pums_gq_persons=meta["n_pums_gq"], ipu_sweeps=len(meta["ipu_hist"]),
                       ipu_mean_rel_error=meta["ipu_hist"][-1],
                       marginal_fit={c: dict(target=float(t), raked=float(f))
                                     for c, t, f in zip(meta["cats"], meta["targets"], meta["fit"])}),
                  f, indent=2)
    print(vdf.to_string(index=False))

    if not a.no_swap:
        os.makedirs(os.path.dirname(old_p), exist_ok=True)
        if os.path.exists(old_p) and not os.path.exists(arch_p):
            os.makedirs(arch_dir, exist_ok=True)
            shutil.move(old_p, arch_p)
            print(f"[pums_population] archived old population -> {arch_p}")
        pop.to_csv(old_p, index=False)
        regenerate_pop_folder(fips, pop)
        print(f"[pums_population] wrote {old_p} and regenerated populations/pop{fips}/")


if __name__ == "__main__":
    main()
