#!/usr/bin/env bash
#SBATCH -A YOUR_PROJECT_ACCOUNT
#SBATCH -p berzelius-hopper
#SBATCH -J sven-myndighet-train
#SBATCH --gpus=1
#SBATCH -c 8
#SBATCH --mem=64G
#SBATCH -t 04:00:00
#SBATCH -o /proj/PROJECT/users/USER/sven-myndighet/logs/train-%j.out
# Delta fine-tune of a Kev-family checkpoint on the myndighetsbeslut typed questions, then heldout scores.
# Settings come from environment variables (sbatch --export=ALL,NAME=...,BASE=...):
#   NAME      run name under runs/                       (required)
#   BASE      base model          default Qwen/Qwen3-0.6B-Base
#   INIT      warm start          default birgermoell/sven-0.6b
#   DATA      data folder         default data_v2
#   EPOCHS    default 2   BATCH default 1   ACCUM default 8   STEPS default 0 (= all)
#   BASEREV   pinned base revision (optional)
set -euo pipefail
B=/proj/PROJECT/users/USER/sven-myndighet
: "${NAME:?set NAME}"
BASE=${BASE:-Qwen/Qwen3-0.6B-Base}; INIT=${INIT:-birgermoell/sven-0.6b}; DATA=${DATA:-data_v2}
EPOCHS=${EPOCHS:-2}; BATCH=${BATCH:-1}; ACCUM=${ACCUM:-8}; STEPS=${STEPS:-0}
OUT=$B/runs/$NAME
export UV_CACHE_DIR=$B/cache/uv UV_PYTHON_INSTALL_DIR=$B/cache/python XDG_CACHE_HOME=$B/cache
export HF_HOME=$B/cache/hf HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
module load uv/0.13.5
cd $B/code/kev
echo "run=$NAME base=$BASE init=$INIT data=$DATA epochs=$EPOCHS batch=$BATCH accum=$ACCUM steps=$STEPS"
# --max_state: Kev's default is 384 tokens, which would drop most of our case files.
uv run --offline python -m kev.train \
    --base "$BASE" ${BASEREV:+--base_revision $BASEREV} --init_from "$INIT" \
    --data $B/$DATA/train.jsonl --max_state 7552 \
    --lr 2e-5 --epochs $EPOCHS --batch $BATCH --accum $ACCUM --dtype bf16 --checkpointing 1 \
    --max_steps $STEPS --seed 0 --device cuda --out $OUT
uv run --offline python $B/slurm/sven_eval.py --run $OUT --data $B/$DATA/heldout.jsonl \
    --out $OUT/eval_heldout.json --device cuda
