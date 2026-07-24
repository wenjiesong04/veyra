#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to run Veyra Desktop in development." >&2
  exit 2
fi

if [ -f "$HOME/.cargo/env" ]; then
  # shellcheck disable=SC1091
  source "$HOME/.cargo/env"
fi

if ! command -v cargo >/dev/null 2>&1; then
  echo "Rust/Cargo is required to run Veyra Desktop in development." >&2
  echo "Install Rust from https://www.rust-lang.org/tools/install, then rerun this script." >&2
  exit 2
fi

if ! command -v rustc >/dev/null 2>&1; then
  echo "rustc is required to prepare the Tauri sidecar stub for development." >&2
  echo "Run: rustup default stable" >&2
  exit 2
fi

if [ ! -f desktop_backend.py ]; then
  echo "desktop_backend.py is required for Veyra Desktop to auto-start the local API." >&2
  exit 2
fi

ensure_dev_sidecar_stub() {
  local triple stub bin_dir
  triple="$(rustc --print host-tuple 2>/dev/null || true)"
  if [ -z "$triple" ]; then
    triple="$(rustc -Vv 2>/dev/null | awk '/^host:/{print $2; exit}')"
  fi
  if [ -z "$triple" ]; then
    echo "Could not determine Rust host triple for the Tauri sidecar stub." >&2
    exit 2
  fi

  bin_dir="$ROOT/apps/desktop/src-tauri/binaries"
  stub="$bin_dir/veyra-backend-${triple}"
  mkdir -p "$bin_dir"

  if [ -f "$stub" ]; then
    return 0
  fi

  cat >"$stub" <<'EOF'
#!/bin/sh
echo "veyra-backend sidecar stub: dev builds run desktop_backend.py from the repo root." >&2
echo "For release packaging, run: python3 scripts/build_desktop_sidecar.py" >&2
exit 1
EOF
  chmod +x "$stub"
  echo "Created dev sidecar stub: $stub"
}

ensure_dev_sidecar_stub

(cd apps/desktop && npm install && npm run dev)
