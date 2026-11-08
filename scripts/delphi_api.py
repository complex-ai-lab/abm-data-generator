"""Fetch daily confirmed cases and deaths for one or more counties from the Delphi
Epidata API (indicator-combination, 2020-06-01 to 2021-07-31) and write
data/delphi_county_data/<FIPS>_data.csv (time_value, geo_value, cases, deaths).
COVIDCAST_API_KEY in .env is used when present; the API also answers without one.

    python scripts/delphi_api.py 01031 13013
"""
import os
import sys

import pandas as pd
import requests
from dotenv import load_dotenv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(REPO, ".env"))
URL = "https://api.delphi.cmu.edu/epidata/covidcast/"
SIGNALS = {"confirmed_incidence_num": "cases", "deaths_incidence_num": "deaths"}


def fetch(fips, signal):
    params = dict(data_source="indicator-combination", signals=signal, time_type="day",
                  geo_type="county", geo_values=fips, time_values="20200601-20210731")
    if os.environ.get("COVIDCAST_API_KEY"):
        params["api_key"] = os.environ["COVIDCAST_API_KEY"]
    r = requests.get(URL, params=params, timeout=120)
    r.raise_for_status()
    js = r.json()
    if js.get("result") != 1 or not js.get("epidata"):
        sys.exit(f"[delphi_api] {fips} {signal}: {js.get('message')}")
    df = pd.DataFrame(js["epidata"])[["time_value", "value"]]
    df["time_value"] = pd.to_datetime(df["time_value"].astype(str), format="%Y%m%d")
    return df.rename(columns={"value": SIGNALS[signal]}).set_index("time_value")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out_dir = os.path.join(REPO, "data", "delphi_county_data")
    os.makedirs(out_dir, exist_ok=True)
    for fips in sys.argv[1:]:
        df = pd.concat([fetch(fips, s) for s in SIGNALS], axis=1).sort_index().reset_index()
        df.insert(1, "geo_value", fips)
        out = os.path.join(out_dir, f"{fips}_data.csv")
        df.to_csv(out, index=False)
        print(f"[delphi_api] {fips}: {len(df)} days -> {out}")


if __name__ == "__main__":
    main()
