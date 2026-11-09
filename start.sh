#!/bin/sh
set -eu
cd "$(dirname "$0")"
# Prefer the Conda environment already activated by the user.
if [ -n "${CONDA_PREFIX:-}" ] && [ -x "$CONDA_PREFIX/bin/python" ]; then
  exec "$CONDA_PREFIX/bin/python" app.py
fi
if command -v conda >/dev/null 2>&1; then
  exec conda run --no-capture-output -n base python app.py
fi
echo "请先在终端激活已有 Conda 环境，再运行 python app.py。" >&2
exit 1
