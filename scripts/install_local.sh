#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON:-python3}"
VENV_DIR="${VEYRA_VENV_DIR:-.venv}"

echo "==> Creating Python virtual environment: $VENV_DIR"
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
. "$VENV_DIR/bin/activate"

echo "==> Installing Python dependencies"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if command -v npm >/dev/null 2>&1; then
  echo "==> Installing and building the local console"
  (cd web && npm install && npm run build)
else
  echo "==> npm is not installed; skipping console build"
  echo "    Install Node.js/npm and run: cd web && npm install && npm run build"
fi

echo "==> Initializing local state directory"
python - <<'PY'
from core.world_state import WorldStateStore

store = WorldStateStore()
print(f"Initialized Veyra state at {store.root}")
PY

if [ ! -f .env ]; then
  echo "==> .env not found"
  echo "    Copy .env.example to .env and fill local model/OpenClaw/Feishu values as needed."
fi

echo "==> Install complete"
echo "Next: ./scripts/start_local.sh --foreground"
