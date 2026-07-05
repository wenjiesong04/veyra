#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -x .venv/bin/python ]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
else
  PYTHON_BIN="${PYTHON:-python3}"
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to build Veyra Desktop." >&2
  exit 2
fi

if ! command -v cargo >/dev/null 2>&1; then
  echo "Rust/Cargo is required to build Veyra Desktop." >&2
  echo "Install Rust from https://www.rust-lang.org/tools/install, then rerun this script." >&2
  exit 2
fi

if ! command -v rustc >/dev/null 2>&1; then
  echo "rustc is required to build the Veyra backend sidecar." >&2
  echo "Install Rust from https://www.rust-lang.org/tools/install, then rerun this script." >&2
  exit 2
fi

echo "==> Building desktop console assets"
(cd web && npm install && npm run build:desktop)

echo "==> Building local backend sidecar"
"$PYTHON_BIN" scripts/build_desktop_sidecar.py

echo "==> Building Veyra desktop package"
(cd apps/desktop && npm install && npm run build)
