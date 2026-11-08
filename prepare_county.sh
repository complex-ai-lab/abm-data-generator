#!/bin/bash
# ---------------------------------------------------------------------------
# Build everything the ABM needs for one county: case data, synthetic
# population, intervention schedule and contact networks.
#
#   bash prepare_county.sh 13013          # needs CENSUS_API_KEY in .env, internet access
#   STEPS=networks bash prepare_county.sh 13013    # rerun only some steps
#   TARGET_N=76182 bash prepare_county.sh 13013    # number of agents (default: ACS county population)
#
# Steps (all run from the repo root, in this order):
#   cases       scripts/delphi_api.py              -> data/delphi_county_data/<FIPS>_data.csv
#   census      scripts/census.py                  -> data/state_data/<ST>/<FIPS>/agents_{ages,household_sizes,occupations}.csv
#   industry    scripts/fetch_industry_c24030.py   -> data/state_data/<ST>/<FIPS>/agents_occupations_c24030.csv
#   population  scripts/pums_population.py         -> data/population_data/<ST>_population_data/<FIPS>_population.csv,
#                                                     populations/pop<FIPS>/{age.pickle,disease_stages.csv}
#   county      scripts/process_counties.py        -> data/processed_data/<FIPS>/202010-202104/daily_data.csv,
#                                                     populations/pop<FIPS>/{intervention.csv,mapping.json}
#   vacc65      scripts/backfill_vaccines_65plus.py-> vaccines_65plus column of intervention.csv
#   burnin      scripts/add_burnin_days.py         -> 14 burn-in rows prepended to intervention.csv
#   networks    networks/build_p0_networks.py      -> data/networks/covid_output_p0/<FIPS>/mobility_networks/ (182 days)
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"
F="${1:?usage: bash prepare_county.sh <FIPS>}"
PY="${PY:-python}"
STEPS="${STEPS:-cases census industry population county vacc65 burnin networks}"
declare -A ST_MAP=( [01]=AL [13]=GA [17]=IL [19]=IA [20]=KS [21]=KY [22]=LA [26]=MI [27]=MN [29]=MO
                    [30]=MT [36]=NY [38]=ND [39]=OH [40]=OK [42]=PA [46]=SD [47]=TN [48]=TX [51]=VA )
ST="${ST_MAP[${F:0:2}]:?no state abbreviation for FIPS prefix ${F:0:2}; add it to ST_MAP here and FIPS_TO_ST in the scripts}"
export TARGET_STATE="$ST" TARGET_COUNTIES="$F"

for step in $STEPS; do
    echo "[prepare] $F ($ST) $step  $(date +%H:%M:%S)"
    case $step in
        cases)      $PY scripts/delphi_api.py "$F";;
        census)     $PY scripts/census.py;;
        industry)   $PY scripts/fetch_industry_c24030.py "$F";;
        population) $PY scripts/pums_population.py "$F" --out-dir "results/$F/population_build" \
                        ${TARGET_N:+--target-n "$TARGET_N"};;
        county)     $PY scripts/process_counties.py;;
        vacc65)     $PY scripts/backfill_vaccines_65plus.py "$F";;
        burnin)     $PY scripts/add_burnin_days.py --days 14 --carry-npi "$F";;
        networks)   $PY networks/build_p0_networks.py "$F" --out-root "$PWD/data/networks/covid_output_p0";;
        *) echo "[prepare] unknown step $step"; exit 1;;
    esac
done
echo "[prepare] $F done  $(date +%H:%M:%S)"
