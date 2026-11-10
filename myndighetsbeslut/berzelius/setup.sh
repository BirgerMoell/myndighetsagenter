#!/usr/bin/env bash
# One-time setup on the Berzelius login node. Everything goes under /proj (home is full).
# Downloads: the public Kev code, its Python environment (torch etc., a few GB) and two small models (~1.3 GB).
set -euo pipefail
B=/proj/PROJECT/users/USER/sven-myndighet
mkdir -p $B/{code,data,runs,logs,slurm,cache/uv,cache/python,cache/hf}
export UV_CACHE_DIR=$B/cache/uv UV_PYTHON_INSTALL_DIR=$B/cache/python UV_LINK_MODE=copy
export XDG_CACHE_HOME=$B/cache HF_HOME=$B/cache/hf
module load uv/0.13.5

cd $B/code
[ -d kev ] || git clone --depth 1 https://github.com/jaredpalmer/kev.git
cd kev
git log --oneline -1
uv sync

# fetch the base model and the Sven adapter now, so the GPU job can run offline
uv run python - <<'PY'
from huggingface_hub import snapshot_download
print(snapshot_download("Qwen/Qwen3-0.6B-Base"))
print(snapshot_download("birgermoell/sven-0.6b"))
PY
echo "setup done: $B"
