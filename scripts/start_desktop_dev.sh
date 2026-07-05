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

python3 - <<'PY'
from urllib.request import urlopen

try:
    with urlopen("http://127.0.0.1:8000/health", timeout=1.5) as response:
        print(f"Veyra API reachable: HTTP {response.status}")
except Exception:
    print("Veyra API is not reachable at http://127.0.0.1:8000.")
    print("Start it first with: ./scripts/start_local.sh --foreground")
    raise SystemExit(2)
PY

(cd apps/desktop && npm install && npm run dev)
