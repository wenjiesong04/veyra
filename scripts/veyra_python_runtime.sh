#!/usr/bin/env bash

# Shared fail-closed Python selection for Veyra's local installer and runtime.
# This file is sourced by other scripts; it intentionally does not change the
# caller's shell options.

veyra_acquire_update_lock() {
  local project_root="$1"
  local purpose="${2:-runtime update}"
  local lock_parent="$project_root/state/local"
  mkdir -p "$lock_parent"

  VEYRA_UPDATE_LOCK_PATH="$lock_parent/.runtime-update.lock"
  VEYRA_UPDATE_LOCK_KIND=""
  if command -v shlock >/dev/null 2>&1; then
    if ! shlock -f "$VEYRA_UPDATE_LOCK_PATH" -p "$$"; then
      echo "Another Veyra install/start operation holds the runtime update lock: $VEYRA_UPDATE_LOCK_PATH" >&2
      return 1
    fi
    VEYRA_UPDATE_LOCK_KIND="shlock"
  else
    VEYRA_UPDATE_LOCK_PATH="${VEYRA_UPDATE_LOCK_PATH}.d"
    if ! mkdir "$VEYRA_UPDATE_LOCK_PATH" 2>/dev/null; then
      local owner_pid=""
      owner_pid="$(sed -n '1p' "$VEYRA_UPDATE_LOCK_PATH/pid" 2>/dev/null || true)"
      if [[ "$owner_pid" =~ ^[0-9]+$ ]] && ! kill -0 "$owner_pid" 2>/dev/null; then
        local stale_path="${VEYRA_UPDATE_LOCK_PATH}.stale.$$"
        if mv "$VEYRA_UPDATE_LOCK_PATH" "$stale_path" 2>/dev/null; then
          rm -rf "$stale_path"
        fi
      fi
      if ! mkdir "$VEYRA_UPDATE_LOCK_PATH" 2>/dev/null; then
        echo "Another Veyra install/start operation holds the runtime update lock: $VEYRA_UPDATE_LOCK_PATH" >&2
        return 1
      fi
    fi
    printf '%s\n' "$$" > "$VEYRA_UPDATE_LOCK_PATH/pid"
    VEYRA_UPDATE_LOCK_KIND="directory"
  fi
  VEYRA_UPDATE_LOCK_PURPOSE="$purpose"
}

veyra_release_update_lock() {
  if [ -z "${VEYRA_UPDATE_LOCK_PATH:-}" ]; then
    return 0
  fi
  if [ "${VEYRA_UPDATE_LOCK_KIND:-}" = "directory" ]; then
    rm -f "$VEYRA_UPDATE_LOCK_PATH/pid"
    rmdir "$VEYRA_UPDATE_LOCK_PATH" 2>/dev/null || true
  else
    rm -f "$VEYRA_UPDATE_LOCK_PATH"
  fi
  VEYRA_UPDATE_LOCK_PATH=""
  VEYRA_UPDATE_LOCK_KIND=""
  VEYRA_UPDATE_LOCK_PURPOSE=""
}

veyra_resolve_python() {
  local project_root="$1"
  local candidate=""
  local source=""

  if [ -n "${VEYRA_PYTHON:-}" ]; then
    candidate="$VEYRA_PYTHON"
    source="VEYRA_PYTHON"
  elif [ "${CONDA_DEFAULT_ENV:-}" = "veyra" ] && [ -n "${CONDA_PREFIX:-}" ]; then
    candidate="$CONDA_PREFIX/bin/python"
    source="conda:veyra"
  elif [ -x "$project_root/.venv/bin/python" ]; then
    candidate="$project_root/.venv/bin/python"
    source=".venv"
  elif [ -n "${PYTHON:-}" ]; then
    candidate="$PYTHON"
    source="PYTHON"
  else
    echo "No Veyra Python environment was selected." >&2
    echo "Activate it with 'conda activate veyra', create the project .venv, or set VEYRA_PYTHON=/absolute/path/to/python." >&2
    return 2
  fi

  if [[ "$candidate" != /* ]]; then
    candidate="$(command -v -- "$candidate" 2>/dev/null || true)"
  fi
  if [ -z "$candidate" ] || [ ! -x "$candidate" ]; then
    echo "Selected Veyra Python is not an executable: ${candidate:-<empty>}" >&2
    return 2
  fi

  local candidate_dir
  candidate_dir="$(cd "$(dirname "$candidate")" && pwd -P)"
  VEYRA_RESOLVED_PYTHON="$candidate_dir/$(basename "$candidate")"
  VEYRA_RESOLVED_PYTHON_SOURCE="$source"
}

veyra_validate_python_environment() {
  local python_bin="$1"
  local project_root="$2"
  local selection_source="$3"
  local expected_prefix="${4:-}"

  "$python_bin" - "$project_root" "$selection_source" "$expected_prefix" <<'PY'
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
source = sys.argv[2]
expected_prefix_arg = sys.argv[3]
executable = Path(sys.executable).resolve()
prefix = Path(sys.prefix).resolve()

if sys.version_info[:2] != (3, 11):
    raise SystemExit(
        f"Veyra requires Python 3.11.x; selected {sys.version.split()[0]} at {executable}"
    )

if source == "conda:veyra":
    conda_prefix = Path(os.environ.get("CONDA_PREFIX", "")).resolve()
    if os.environ.get("CONDA_DEFAULT_ENV") != "veyra" or prefix != conda_prefix or prefix.name != "veyra":
        raise SystemExit(f"Selected Conda interpreter is not the active veyra environment: {prefix}")
elif source == ".venv":
    expected = (root / ".venv").resolve()
    if prefix != expected:
        raise SystemExit(f"Project .venv interpreter resolved outside {expected}: {prefix}")
elif source in {"explicit_existing_venv", "explicit_created_venv"}:
    if not expected_prefix_arg:
        raise SystemExit(f"Expected virtual-environment prefix is required for {source}")
    expected = Path(expected_prefix_arg).resolve()
    if prefix != expected:
        raise SystemExit(f"Selected virtual-environment interpreter resolved outside {expected}: {prefix}")

print(
    json.dumps(
        {
            "python_executable": str(executable),
            "python_version": sys.version.split()[0],
            "environment": str(prefix),
            "selection_source": source,
        },
        sort_keys=True,
    )
)
PY
}

veyra_runtime_info() {
  local python_bin="$1"
  local project_root="$2"
  local selection_source="$3"
  local expected_prefix="${4:-}"

  veyra_validate_python_environment "$python_bin" "$project_root" "$selection_source" "$expected_prefix" >/dev/null || return $?
  "$python_bin" - "$project_root" "$selection_source" <<'PY'
import importlib
import json
import os
import ssl
import sys
from importlib import metadata
from pathlib import Path

root = Path(sys.argv[1]).resolve()
source = sys.argv[2]
requirements_path = root / "requirements.txt"
if not requirements_path.is_file():
    raise SystemExit(f"Missing runtime requirements file: {requirements_path}")

expected: dict[str, str] = {}
for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.split("#", 1)[0].strip()
    if not line or "==" not in line:
        continue
    name, pinned = line.split("==", 1)
    expected[name.split("[", 1)[0].strip()] = pinned.strip()

mismatches: list[str] = []
for name, pinned in expected.items():
    try:
        installed = metadata.version(name)
    except metadata.PackageNotFoundError:
        mismatches.append(f"{name}=missing (expected {pinned})")
        continue
    if installed != pinned:
        mismatches.append(f"{name}={installed} (expected {pinned})")
if mismatches:
    raise SystemExit("Veyra runtime dependencies are not synchronized: " + "; ".join(mismatches))

for module_name in ("fastapi", "uvicorn", "cryptography", "lark_oapi", "certifi"):
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        raise SystemExit(f"Veyra runtime dependency import failed for {module_name}: {type(exc).__name__}: {exc}") from exc

explicit_ca_keys = (
    "FEISHU_CA_BUNDLE",
    "LARK_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
)
for key in explicit_ca_keys:
    value = str(os.environ.get(key) or "").strip()
    if value and not Path(value).expanduser().is_file():
        raise SystemExit(f"{key} does not point to a readable CA bundle: {value}")

verify_paths = ssl.get_default_verify_paths()
ca_file = next(
    (
        str(Path(str(os.environ.get(key))).expanduser())
        for key in explicit_ca_keys
        if str(os.environ.get(key) or "").strip()
    ),
    str(verify_paths.cafile or ""),
)
if sys.platform == "darwin" and (not ca_file or not Path(ca_file).is_file()):
    raise SystemExit("Veyra Python has no readable TLS CA file on macOS.")
if ca_file:
    try:
        tls_context = ssl.create_default_context(cafile=ca_file)
    except Exception as exc:
        raise SystemExit(
            f"Veyra Python could not load the selected TLS CA bundle: {type(exc).__name__}: {exc}"
        ) from exc
    if not tls_context.get_ca_certs():
        raise SystemExit("Veyra Python loaded no trusted certificates from the selected TLS CA bundle.")

print(
    json.dumps(
        {
            "python_executable": str(Path(sys.executable).resolve()),
            "python_version": sys.version.split()[0],
            "environment": str(Path(sys.prefix).resolve()),
            "selection_source": source,
            "ca_file": ca_file or None,
            "requirements_verified": sorted(expected),
        },
        sort_keys=True,
    )
)
PY
}
