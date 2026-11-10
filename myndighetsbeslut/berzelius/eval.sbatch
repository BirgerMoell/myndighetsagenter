#!/usr/bin/env bash
#SBATCH -A YOUR_PROJECT_ACCOUNT
#SBATCH -p berzelius-hopper
#SBATCH -J sven-myndighet-eval
#SBATCH --gpus=1
#SBATCH -c 8
#SBATCH --mem=48G
#SBATCH -t 00:45:00
#SBATCH -o /proj/PROJECT/users/USER/sven-myndighet/logs/eval-%j.out
# Score a checkpoint on the heldout split.
#   sbatch eval.sbatch                                  -> birgermoell/sven-0.6b on data_v2
#   sbatch eval.sbatch jaredpalmer/kev-4b               -> another hub checkpoint
#   sbatch eval.sbatch runs/sven-06b-v2 data_v2         -> a trained run
set -euo pipefail
B=/proj/PROJECT/users/USER/sven-myndighet
RUN=${1:-birgermoell/sven-0.6b}; DATA=${2:-data_v2}
export UV_CACHE_DIR=$B/cache/uv UV_PYTHON_INSTALL_DIR=$B/cache/python XDG_CACHE_HOME=$B/cache
export HF_HOME=$B/cache/hf HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
module load uv/0.13.5
cd $B/code/kev
case "$RUN" in runs/*) RUN=$B/$RUN ;; esac
name=$(echo "$RUN" | sed "s|$B/||" | tr '/' '_')
uv run --offline python $B/slurm/sven_eval.py --run "$RUN" --data $B/$DATA/heldout.jsonl \
    --out $B/runs/eval_${name}_${DATA}_${SLURM_JOB_ID}.json --device cuda
