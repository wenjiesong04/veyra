#!/usr/bin/env bash
set -euo pipefail

# Build the local Apple Silicon Product Preview. Dependencies are verified,
# never installed implicitly; prepare the selected environment separately.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/scripts/veyra_python_runtime.sh"
# Prefer an already exported Cargo toolchain. If it is not on PATH, load the
# portable rustup environment selected by CARGO_HOME (without a user path).
if ! command -v cargo >/dev/null 2>&1 || ! command -v rustc >/dev/null 2>&1; then
  CARGO_ENV="${CARGO_HOME:-$HOME/.cargo}/env"
  if [ -f "$CARGO_ENV" ]; then
    # shellcheck disable=SC1090
    source "$CARGO_ENV"
  fi
fi

COMMAND="package"
REQUIRE_CLEAN="${VEYRA_DESKTOP_REQUIRE_CLEAN:-1}"
for argument in "$@"; do
  case "$argument" in
    package|full|sidecar|sidecar-smoke|manifest)
      COMMAND="$argument"
      ;;
    --require-clean)
      REQUIRE_CLEAN="1"
      ;;
    --allow-dirty)
      REQUIRE_CLEAN="0"
      ;;
    --help|-h)
      cat <<'USAGE'
Usage: scripts/build_desktop.sh [package|sidecar|sidecar-smoke|manifest] [--require-clean|--allow-dirty]

package (default)  Require a clean tree, then build web assets, arm64 sidecar,
                   and an app-only
                   Tauri bundle. It prints an auditable manifest and verifies
                   the bundled sidecar byte-for-byte.
sidecar            Build only the arm64 sidecar; useful on a dirty worktree.
sidecar-smoke      Start the existing sidecar with isolated temporary state and
                   verify GET /setup/status, then terminate it.
manifest           Print the current build/runtime manifest without building.

No command installs npm, Rust, Python, or PyInstaller dependencies.
USAGE
      exit 0
      ;;
    *)
      echo "Unknown desktop build argument: $argument" >&2
      exit 2
      ;;
  esac
done

resolve_supported_python() {
  veyra_resolve_python "$ROOT"
  PYTHON_BIN="$VEYRA_RESOLVED_PYTHON"
  PYTHON_SOURCE="$VEYRA_RESOLVED_PYTHON_SOURCE"
  veyra_validate_python_environment "$PYTHON_BIN" "$ROOT" "$PYTHON_SOURCE" ""
  export PYTHON_BIN PYTHON_SOURCE
}

require_arm64_macos() {
  if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
    echo "Veyra Desktop Preview packaging supports only Apple Silicon macOS (arm64)." >&2
    exit 2
  fi
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "$1 is required; install it separately, then rerun this command." >&2
    exit 2
  fi
}

require_node_dependencies() {
  require_command node
  require_command npm
  if [ ! -x "$ROOT/web/node_modules/.bin/vite" ]; then
    echo "web dependencies are missing; run npm install/npm ci manually in web/." >&2
    exit 2
  fi
  if [ ! -x "$ROOT/apps/desktop/node_modules/.bin/tauri" ]; then
    echo "desktop dependencies are missing; run npm install/npm ci manually in apps/desktop/." >&2
    exit 2
  fi
}

require_rust() {
  require_command rustc
  require_command cargo
  local triple
  triple="$(rustc --print host-tuple 2>/dev/null || true)"
  if [ "$triple" != "aarch64-apple-darwin" ]; then
    echo "Rust host target must be aarch64-apple-darwin; got ${triple:-<unknown>}." >&2
    exit 2
  fi
}

require_clean_tree() {
  if [ "$REQUIRE_CLEAN" = "1" ] && [ -n "$(git status --porcelain --untracked-files=all)" ]; then
    echo "Desktop package build requires a clean worktree; use --allow-dirty only for local preview work." >&2
    exit 2
  fi
}

sidecar_path() {
  printf '%s\n' "$ROOT/apps/desktop/src-tauri/binaries/veyra-backend-aarch64-apple-darwin"
}

build_sidecar() {
  resolve_supported_python
  require_rust
  "$PYTHON_BIN" scripts/build_desktop_sidecar.py
}

run_sidecar_smoke() {
  resolve_supported_python
  local binary
  binary="$(sidecar_path)"
  if [ ! -f "$binary" ]; then
    echo "Sidecar is missing: $binary (run the sidecar command first)." >&2
    exit 2
  fi
  "$PYTHON_BIN" scripts/desktop_sidecar_smoke.py --binary "$binary"
}

build_app() {
  require_node_dependencies
  # `app` is the only target for this local preview. The explicit ad-hoc
  # identity is not Developer ID signing, notarization, DMG, or distribution.
  export APPLE_SIGNING_IDENTITY="-"
  (cd "$ROOT/web" && npm run build:desktop)
  # Override the legacy beforeBuildCommand so packaging never runs npm ci;
  # frontend assets were built and dependency presence was checked above.
  (cd "$ROOT/apps/desktop" && npm run build -- --bundles app --config '{"build":{"beforeBuildCommand":""}}')
}

find_app_bundle() {
  find "$ROOT/apps/desktop/src-tauri/target/release/bundle/macos" -maxdepth 1 -type d -name '*.app' -print -quit 2>/dev/null || true
}

verify_packaged_sidecar() {
  local app_path="$1"
  require_command codesign
  local source_sidecar
  source_sidecar="$(sidecar_path)"
  if [ ! -f "$source_sidecar" ]; then
    echo "Source sidecar is missing: $source_sidecar" >&2
    exit 1
  fi
  local packaged_sidecar
  packaged_sidecar="$(find "$app_path/Contents" -type f -name 'veyra-backend*' -print -quit 2>/dev/null || true)"
  if [ -z "$packaged_sidecar" ]; then
    echo "No sidecar was found inside $app_path." >&2
    exit 1
  fi

  # Tauri signs the copy embedded in the app.  Compare unsigned temporary
  # copies so the check verifies the payload rather than the expected
  # signature metadata.  Never mutate the source sidecar or the app bundle.
  local compare_dir
  compare_dir="$(mktemp -d "${TMPDIR:-/tmp}/veyra-sidecar-compare.XXXXXX")"
  local source_copy="$compare_dir/source"
  local packaged_copy="$compare_dir/packaged"
  cp "$source_sidecar" "$source_copy"
  cp "$packaged_sidecar" "$packaged_copy"
  # The PyInstaller source may already be unsigned; codesign returns a
  # non-zero status in that case, which is safe to ignore for this strip step.
  codesign --remove-signature "$source_copy" >/dev/null 2>&1 || true
  codesign --remove-signature "$packaged_copy" >/dev/null 2>&1 || true
  if ! cmp -s "$source_copy" "$packaged_copy"; then
    rm -rf "$compare_dir"
    echo "Packaged sidecar differs from the built arm64 sidecar: $packaged_sidecar" >&2
    exit 1
  fi
  rm -rf "$compare_dir"
  echo "Verified packaged sidecar byte-for-byte: $packaged_sidecar"
}

verify_app_signature() {
  local app_path="$1"
  require_command codesign
  codesign --verify --deep --strict "$app_path"
  echo "Verified ad-hoc app signature (not notarized): $app_path"
}

print_manifest() {
  "$PYTHON_BIN" - "$ROOT" "$(sidecar_path)" "${1:-}" "$COMMAND" <<'PY'
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
sidecar = Path(sys.argv[2])
app = Path(sys.argv[3]) if sys.argv[3] else None
command = sys.argv[4]

def command_version(*args: str) -> str | None:
    try:
        result = subprocess.run(args, text=True, capture_output=True, check=False)
    except OSError:
        return None
    values = (result.stdout or result.stderr).strip().splitlines()
    return values[0] if values else None

def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def tree_sha256(path: Path) -> str | None:
    if not path.is_dir():
        return None
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(str(child.relative_to(path)).encode())
        digest.update(b"\0")
        with child.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()

try:
    version = json.loads((root / "apps/desktop/package.json").read_text())
except Exception:
    version = {}
git_head = command_version("git", "rev-parse", "HEAD")
dirty = bool(command_version("git", "status", "--porcelain", "--untracked-files=all"))
pyinstaller = importlib.metadata.version("pyinstaller") if importlib.util.find_spec("PyInstaller") else None
manifest = {
    "command": command,
    "git_head": git_head,
    "dirty": dirty,
    "version": version.get("version"),
    "target": "aarch64-apple-darwin",
    "python": sys.version.split()[0],
    "python_executable": str(Path(sys.executable).resolve()),
    "pyinstaller": pyinstaller,
    "node": command_version("node", "--version"),
    "rust": command_version("rustc", "--version"),
    "tauri": command_version(str(root / "apps/desktop/node_modules/.bin/tauri"), "--version"),
    "signing": "ad_hoc_not_notarized_local_preview",
    "sidecar": {"path": str(sidecar), "sha256": sha256(sidecar)},
    "app": {"path": str(app) if app else None, "sha256": tree_sha256(app) if app else None},
}
print(json.dumps(manifest, sort_keys=True))
PY
}

if [ "$COMMAND" != "manifest" ]; then
  require_arm64_macos
fi

case "$COMMAND" in
  manifest)
    resolve_supported_python
    print_manifest
    ;;
  sidecar)
    build_sidecar
    print_manifest
    ;;
  sidecar-smoke)
    run_sidecar_smoke
    print_manifest
    ;;
  package|full)
    require_clean_tree
    require_node_dependencies
    build_sidecar
    run_sidecar_smoke
    build_app
    APP_PATH="$(find_app_bundle)"
    if [ -z "$APP_PATH" ]; then
      echo "Tauri app bundle was not created under src-tauri/target/release/bundle/macos." >&2
      exit 1
    fi
    verify_app_signature "$APP_PATH"
    verify_packaged_sidecar "$APP_PATH"
    print_manifest "$APP_PATH"
    ;;
esac
