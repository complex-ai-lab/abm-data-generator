"""Copy a run.sh output into the reference_results format (called by run.sh after a factual run).

    .venv/bin/python export_reference.py results/01031/simple reference_results_simple/01031 \
        result_graphs/01031/.../calibrated_params.txt

<ref_dir> gets the same files as reference_results/<FIPS>/:
  config_<FIPS>.yaml          the run's config with R_T, INITIAL_INFECTED and K filled in with the values the
                              run used (config values where set, else the calibration), so
                              CONFIG=<ref_dir>/config_<FIPS>.yaml bash run.sh reruns it without a calibration
  r_t_daily.csv, seirv_daily.csv, cases_sim_vs_observed.csv, seir_rates_daily.csv
"""
import os
import re
import shutil
import sys

import numpy as np
import yaml

CSVS = ("r_t_daily.csv", "seirv_daily.csv", "cases_sim_vs_observed.csv", "seir_rates_daily.csv")


def main(run_dir, ref_dir, params_file):
    text = open(os.path.join(run_dir, "config_used.yaml")).read()
    cfg = yaml.safe_load(text)
    epi = cfg["epidemic"]
    # config_used.yaml is the raw config, whose POPULATION / population_dir / num_agents are the template
    # county's (main.py sets them at run time); take the county from the run folder results/<FIPS>/<RUN_NAME>.
    fips = os.path.basename(os.path.dirname(os.path.abspath(run_dir)))
    if not re.fullmatch(r"\d{5}", fips):
        sys.exit(f"[export] cannot read a 5-digit FIPS from {run_dir} (expected results/<FIPS>/<RUN_NAME>)")
    stages = os.path.join("populations", f"pop{fips}", "disease_stages.csv")
    if not os.path.exists(stages):
        sys.exit(f"[export] missing {stages}")
    with open(stages) as f:
        n_agents = sum(1 for _ in f) - 1
    for key, val in (("POPULATION", f"'{fips}'"), ("population_dir", f"populations/pop{fips}"),
                     ("num_agents", str(n_agents))):
        text, n = re.subn(rf"(?m)^(  {key}: )\S+", rf"\g<1>{val}", text)
        if n != 1:
            sys.exit(f"[export] expected one '  {key}:' line in {run_dir}/config_used.yaml, found {n}")
    params = np.loadtxt(params_file).ravel() if os.path.exists(params_file) else None

    def need(name):
        if params is None:
            sys.exit(f"[export] {name} is null in the config and there is no calibration at {params_file}")
        return params

    if epi.get("R_T") is None and epi.get("R_T_DAILY") is None:
        r_t = ", ".join(f"{v:.6f}" for v in need("R_T")[:-2])
        text = re.sub(r"(?m)^  R_T: null.*$", f"  R_T: [{r_t}]   # calibrated, weeks 0..{len(params) - 3}", text)
    if epi.get("INITIAL_INFECTED") is None:
        text = re.sub(r"(?m)^  INITIAL_INFECTED: null.*$",
                      f"  INITIAL_INFECTED: {need('INITIAL_INFECTED')[-2]:.6f}             "
                      f"# fraction infected at day 0 (calibrated)", text)
    if epi.get("K") is None:
        text = re.sub(r"(?m)^  K: null.*$",
                      f"  K: {need('K')[-1]:.6f}                            "
                      f"# reporting fraction (cases = k x new infections) (calibrated)", text)
    header = (f"# Config of the factual run (CF 7) behind {ref_dir}/ (run.sh output {run_dir}).\n"
              f"# R_T, INITIAL_INFECTED and K are the values that run used. Rerun with\n"
              f"#   CONFIG={ref_dir}/config_{fips}.yaml RUN_NAME=ref bash run.sh\n")
    usage = re.compile(r"(?m)^#   1\) bash calibrate\.sh.*\n#   2\) bash run\.sh.*\n# Edit the EPIDEMIC.*\n")
    text = usage.sub(header, text, count=1) if usage.search(text) else text

    os.makedirs(ref_dir, exist_ok=True)
    with open(os.path.join(ref_dir, f"config_{fips}.yaml"), "w") as f:
        f.write(text)
    for name in CSVS:
        shutil.copy2(os.path.join(run_dir, name), os.path.join(ref_dir, name))
    print(f"[export] {run_dir} -> {ref_dir}: config_{fips}.yaml, {', '.join(CSVS)}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
