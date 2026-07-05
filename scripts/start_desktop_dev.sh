#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to run Veyra Desktop in development." >&2
  exit 2
fi

if ! command -v cargo >/dev/null 2>&1; then
  echo "Rust/Cargo is required to run Veyra Desktop in development." >&2
  echo "Install Rust from https://www.rust-lang.org/tools/install, then rerun this script." >&2
  exit 2
fi

if [ ! -f desktop_backend.py ]; then
  echo "desktop_backend.py is required for Veyra Desktop to auto-start the local API." >&2
  exit 2
fi

(cd apps/desktop && npm install && npm run dev)
