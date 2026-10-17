#!/usr/bin/env bash
# Provision an isolated model-service environment on a Linux GPU server.
#
# Everything (uv, Python, venv, caches) is created under the workspace; no apt,
# conda, or system pip is touched. Upload the repository first and extract it
# into the workspace, then run this script there.
#
# Example:
#   TK_WORKSPACE="$HOME/LLM Agent Communication/YLJ" \
#   TK_MODEL_DIR="$HOME/LLM Agent Communication/LLM Model/Llama-3.2-1B-Instruct" \
#   TK_REPO="$TK_WORKSPACE/src/token-kvcache" \
#   bash scripts/provision_server_env.sh
set -uo pipefail

TK_WORKSPACE="${TK_WORKSPACE:-$HOME/LLM Agent Communication/YLJ}"
TK_REPO="${TK_REPO:-$TK_WORKSPACE/src/token-kvcache}"
TK_MODEL_DIR="${TK_MODEL_DIR:-}"
TK_PYTHON="${TK_PYTHON:-3.12}"
TK_TORCH="${TK_TORCH:-2.8.0}"
TK_TRANSFORMERS="${TK_TRANSFORMERS:-4.57.6}"
TK_INDEX="${TK_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
TK_SERVICE_PORT="${TK_SERVICE_PORT:-18100}"
TK_DEVICE="${TK_DEVICE:-cuda:0}"
TK_DTYPE="${TK_DTYPE:-bfloat16}"

export UV_PYTHON_INSTALL_DIR="$TK_WORKSPACE/python"
export UV_CACHE_DIR="$TK_WORKSPACE/cache/uv"
export UV_TOOL_DIR="$TK_WORKSPACE/bin"
export PIP_CACHE_DIR="$TK_WORKSPACE/cache/pip"
export XDG_CACHE_HOME="$TK_WORKSPACE/cache"
export TMPDIR="$TK_WORKSPACE/tmp"

echo "=== workspace ==="
mkdir -p "$TK_WORKSPACE/bin" "$TK_WORKSPACE/python" "$TK_WORKSPACE/cache" \
  "$TK_WORKSPACE/tmp" "$TK_WORKSPACE/logs"
echo "workspace=$TK_WORKSPACE repo=$TK_REPO model=$TK_MODEL_DIR"

echo "=== uv ==="
if [ ! -x "$TK_WORKSPACE/bin/uv" ]; then
  curl -L --fail --max-time 600 -o "$TK_WORKSPACE/tmp/uv.tar.gz" \
    https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz
  tar -xzf "$TK_WORKSPACE/tmp/uv.tar.gz" -C "$TK_WORKSPACE/tmp"
  cp "$TK_WORKSPACE/tmp"/uv-x86_64-unknown-linux-gnu/uv "$TK_WORKSPACE/bin/uv"
  chmod +x "$TK_WORKSPACE/bin/uv"
fi
"$TK_WORKSPACE/bin/uv" --version

echo "=== python $TK_PYTHON ==="
"$TK_WORKSPACE/bin/uv" python install "$TK_PYTHON" 2>&1 | tail -n 3

echo "=== venv ==="
"$TK_WORKSPACE/bin/uv" venv "$TK_WORKSPACE/.venv" --python "$TK_PYTHON" 2>&1 | tail -n 3
PY="$TK_WORKSPACE/.venv/bin/python"
"$PY" --version

echo "=== service deps ==="
"$TK_WORKSPACE/bin/uv" pip install --python "$PY" --index-url "$TK_INDEX" \
  "fastapi>=0.115,<1" "uvicorn[standard]>=0.30,<1" "pydantic>=2.9,<3" \
  "pydantic-settings>=2.6,<3" "pyyaml>=6.0,<7" "httpx>=0.27,<1" 2>&1 | tail -n 4

echo "=== torch ==="
"$TK_WORKSPACE/bin/uv" pip install --python "$PY" --index-url "$TK_INDEX" \
  "torch==$TK_TORCH" 2>&1 | tail -n 6

echo "=== transformers ==="
"$TK_WORKSPACE/bin/uv" pip install --python "$PY" --index-url "$TK_INDEX" \
  "transformers==$TK_TRANSFORMERS" 2>&1 | tail -n 4

echo "=== verify ==="
"$PY" - <<'PYEOF'
import torch
import transformers

print("torch", torch.__version__, "cuda", torch.version.cuda)
print("transformers", transformers.__version__)
print("cuda_available", torch.cuda.is_available(), "count", torch.cuda.device_count())
if torch.cuda.is_available():
    print("gpu0", torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0))
    print("arch_list", torch.cuda.get_arch_list())
PYEOF

echo "=== launcher ==="
cat > "$TK_WORKSPACE/run-model-service.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$TK_REPO"
export MODEL_BACKEND=huggingface
export HF_MODEL_ID="$TK_MODEL_DIR"
export HF_LOCAL_FILES_ONLY=true
export MODEL_SERVICE_HOST="127.0.0.1"
export MODEL_SERVICE_PORT="\${MODEL_SERVICE_PORT:-$TK_SERVICE_PORT}"
export HF_DEVICE="\${HF_DEVICE:-$TK_DEVICE}"
export HF_DTYPE="\${HF_DTYPE:-$TK_DTYPE}"
export HF_PREFILL_CHUNK_TOKENS="\${HF_PREFILL_CHUNK_TOKENS:-1024}"
export HF_MAX_CONTEXT_TOKENS="\${HF_MAX_CONTEXT_TOKENS:-8192}"
export PYTHONPATH="$TK_REPO"
exec "$TK_WORKSPACE/.venv/bin/python" -m model_service
EOF
chmod +x "$TK_WORKSPACE/run-model-service.sh"
cat "$TK_WORKSPACE/run-model-service.sh"

echo "=== done: start with setsid nohup $TK_WORKSPACE/run-model-service.sh & ==="
