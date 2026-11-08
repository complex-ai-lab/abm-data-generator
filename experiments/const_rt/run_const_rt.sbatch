#!/bin/bash
# Constant-R_t, no-vaccine runs (config_simple.yaml) for every row of experiments/const_rt/manifest.csv.
# R_t / I0 / K come from the manifest via R_T_OVERRIDE / I0_OVERRIDE / K_OVERRIDE (no calibration).
# Burn-in R_t stays at the default 1.0. Submit from the repo root:
#   sbatch experiments/const_rt/run_const_rt.sbatch
# Optional: ROWS="1 2" (1-based manifest rows) or CF_ITERS=1 for a smoke test.
#SBATCH --job-name=abm_const_rt
#SBATCH --account=alrodri0
#SBATCH --partition=spgpu
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=64G
#SBATCH --time=04:00:00
#SBATCH --output=logs/%x-%j.log

set -uo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
module load python/3.11.5
export CPATH=/sw/pkgs/arc/python/3.11.5/include/python3.11
export NETWORKS_DIR=/nfs/turbo/coe-alrodri/pkbiswas/data/networks/covid_output_p0
export PY="$PWD/.venv/bin/python"
export CONFIG=config_simple.yaml
export CF_ITERS="${CF_ITERS:-5}"
export OVERWRITE=1
MANIFEST=experiments/const_rt/manifest.csv

nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
"$PY" -c "import torch, sys; ok = torch.cuda.is_available(); print('torch', torch.__version__, 'cuda available:', ok); sys.exit(0 if ok else 1)" \
    || { echo "[abort] torch cannot use the GPU"; exit 1; }

n_rows=$(($(wc -l < "$MANIFEST") - 1))
ROWS="${ROWS:-$(seq 1 "$n_rows")}"
failed=()
for i in $ROWS; do
    # columns: fips,setting,r_t,mu,sigma,n_kept,p5,p95,i0,k,draw_seed
    IFS=, read -r fips setting r_t _ _ _ _ _ i0 k _ < <(sed -n "$((i + 1))p" "$MANIFEST")
    echo "=================== row $i: $fips $setting r_t=$r_t i0=$i0 k=$k  $(date)"
    # Same values as a calibrated_params.txt (26 weekly R_t, I0, K): run.sh passes it on, and
    # export_reference.py needs it to fill R_T / INITIAL_INFECTED / K into the exported config.
    pfile="experiments/const_rt/params/${fips}_${setting}.txt"
    mkdir -p "$(dirname "$pfile")"
    { for _ in $(seq 26); do echo "$r_t"; done; echo "$i0"; echo "$k"; } > "$pfile"
    if FIPS="$fips" RUN_NAME="const_rt_$setting" CALIBRATION="$pfile" \
       R_T_OVERRIDE="$r_t" I0_OVERRIDE="$i0" K_OVERRIDE="$k" \
       REF_OUT="experiments/const_rt/reference_format/$setting/$fips" bash run.sh; then
        echo "[ok] row $i $fips $setting"
    else
        echo "[FAILED] row $i $fips $setting"; failed+=("$i:$fips:$setting")
    fi
done
echo "=================== done $(date); failed: ${failed[*]:-none}"
