#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV_DIR="${VEYRA_VENV_DIR:-.venv}"
if [[ "$VENV_DIR" = /* ]]; then
  VENV_PATH="$VENV_DIR"
else
  VENV_PATH="$ROOT/$VENV_DIR"
fi

# shellcheck disable=SC1091
. "$ROOT/scripts/veyra_python_runtime.sh"
veyra_acquire_update_lock "$ROOT" "local installation"

UI_BUILD_TMP=""
UI_BACKUP=""
PRESERVE_UI_BACKUP=0
cleanup_install() {
  [ -z "$UI_BUILD_TMP" ] || rm -rf "$UI_BUILD_TMP"
  if [ -n "$UI_BACKUP" ] && [ -d "$UI_BACKUP" ] && [ ! -e "$ROOT/ui/console" ]; then
    if ! mv "$UI_BACKUP" "$ROOT/ui/console"; then
      PRESERVE_UI_BACKUP=1
      echo "Previous console preserved for manual recovery at: $UI_BACKUP" >&2
    else
      UI_BACKUP=""
    fi
  fi
  if [ -n "$UI_BACKUP" ]; then
    if [ "$PRESERVE_UI_BACKUP" = "1" ]; then
      echo "Previous console remains available at: $UI_BACKUP" >&2
    else
      rm -rf "$UI_BACKUP"
    fi
  fi
  veyra_release_update_lock
}
trap cleanup_install EXIT
if [ -f "$ROOT/.env" ]; then
  chmod 600 "$ROOT/.env"
fi

if [ -x "$VENV_PATH/bin/python" ] && [ -z "${VEYRA_PYTHON:-}" ] && [ "${CONDA_DEFAULT_ENV:-}" != "veyra" ]; then
  PYTHON_BIN="$VENV_PATH/bin/python"
  PYTHON_SOURCE="explicit_existing_venv"
  if ! veyra_validate_python_environment "$PYTHON_BIN" "$ROOT" "$PYTHON_SOURCE" "$VENV_PATH" >/dev/null; then
    echo "Existing virtual environment is not a self-contained Veyra Python 3.11 environment: $VENV_PATH" >&2
    echo "Move it aside, then rerun with PYTHON=/absolute/path/to/python3.11 to create a new project environment." >&2
    exit 2
  fi
elif [ -n "${VEYRA_PYTHON:-}" ] || [ "${CONDA_DEFAULT_ENV:-}" = "veyra" ]; then
  veyra_resolve_python "$ROOT"
  PYTHON_BIN="$VEYRA_RESOLVED_PYTHON"
  PYTHON_SOURCE="$VEYRA_RESOLVED_PYTHON_SOURCE"
elif [ -n "${PYTHON:-}" ]; then
  veyra_resolve_python "$ROOT"
  BOOTSTRAP_PYTHON="$VEYRA_RESOLVED_PYTHON"
  veyra_validate_python_environment "$BOOTSTRAP_PYTHON" "$ROOT" "$VEYRA_RESOLVED_PYTHON_SOURCE" >/dev/null
  echo "==> Creating Python virtual environment: $VENV_PATH"
  "$BOOTSTRAP_PYTHON" -m venv "$VENV_PATH"
  PYTHON_BIN="$VENV_PATH/bin/python"
  PYTHON_SOURCE="explicit_created_venv"
else
  echo "No Veyra Python environment was selected for installation." >&2
  echo "Activate it with 'conda activate veyra', or set PYTHON=/absolute/path/to/python3.11 to create $VENV_DIR." >&2
  exit 2
fi

veyra_validate_python_environment "$PYTHON_BIN" "$ROOT" "$PYTHON_SOURCE" "$VENV_PATH" >/dev/null
echo "==> Installing into validated Python: $PYTHON_BIN"

SELECTED_EXECUTABLE="$("$PYTHON_BIN" -c 'from pathlib import Path; import sys; print(Path(sys.executable).resolve())')"
process_executable() {
  local process_pid="$1"
  if [ "$(uname -s)" = "Darwin" ] && command -v lsof >/dev/null 2>&1; then
    { lsof -a -p "$process_pid" -d txt -Fn 2>/dev/null || true; } | sed -n 's/^n//p' | head -n 1
  elif [ -e "/proc/$process_pid/exe" ]; then
    readlink "/proc/$process_pid/exe" 2>/dev/null || true
  fi
}

refuse_live_selected_interpreter() {
  local process_pid="$1"
  local process_source="$2"
  local running_executable=""
  running_executable="$(process_executable "$process_pid")"
  if [ -z "$running_executable" ]; then
    if kill -0 "$process_pid" 2>/dev/null; then
      echo "Could not identify the executable for active Veyra $process_source PID $process_pid; refusing an in-place install." >&2
      exit 1
    fi
    return 0
  fi
  if [ -n "$running_executable" ] && [ "$running_executable" = "$SELECTED_EXECUTABLE" ]; then
    echo "Veyra is currently using the Python environment being updated ($process_source, PID $process_pid): $SELECTED_EXECUTABLE" >&2
    echo "Stop that Veyra runtime before installing dependencies, then restart it after installation." >&2
    exit 1
  fi
}

if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
  SERVICE_TARGET="gui/$(id -u)/ai.veyra.api"
  SERVICE_STATE=""
  SERVICE_PID=""
  if SERVICE_STATE="$(launchctl print "$SERVICE_TARGET" 2>&1)"; then
    SERVICE_PID="$(printf '%s\n' "$SERVICE_STATE" | awk '$1 == "pid" && $2 == "=" {print $3; exit}')"
  elif ! printf '%s\n' "$SERVICE_STATE" | grep -Fq "Could not find service"; then
    echo "Could not determine whether Veyra is using the selected Python environment; refusing an in-place install." >&2
    printf '%s\n' "$SERVICE_STATE" >&2
    exit 1
  fi
  if [[ "$SERVICE_PID" =~ ^[0-9]+$ ]]; then
    refuse_live_selected_interpreter "$SERVICE_PID" "LaunchAgent"
  elif [ -n "$SERVICE_STATE" ]; then
    echo "The Veyra LaunchAgent is loaded without a stable PID; stop it before installing dependencies." >&2
    exit 1
  fi
fi

SELECTED_STATE_ROOT="$(
  "$PYTHON_BIN" - "$ROOT" <<'PY'
import os
import sys
from pathlib import Path

from core.env_loader import load_runtime_env

load_runtime_env()
root = Path(sys.argv[1])
explicit = os.getenv("VEYRA_STATE_DIR") or os.getenv("VEYRA_STATE_ROOT")
if explicit:
    selected = Path(explicit).expanduser()
else:
    env = os.getenv("VEYRA_ENV", "").strip().lower()
    selected = root / "state" / env if env in {"dev", "prod", "test"} else root / "state"
print(selected.resolve())
PY
)"
WRITER_LOCK="$SELECTED_STATE_ROOT/.veyra-writer.lock"
if [ -f "$WRITER_LOCK" ]; then
  WRITER_PID="$(
    "$PYTHON_BIN" - "$WRITER_LOCK" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    payload = {}
pid = payload.get("pid")
print(pid if isinstance(pid, int) and pid > 0 else "")
PY
  )"
  if [[ "$WRITER_PID" =~ ^[0-9]+$ ]] && kill -0 "$WRITER_PID" 2>/dev/null; then
    refuse_live_selected_interpreter "$WRITER_PID" "state-writer lease"
  fi
fi

if command -v lsof >/dev/null 2>&1; then
  while IFS= read -r listener_pid; do
    [[ "$listener_pid" =~ ^[0-9]+$ ]] || continue
    refuse_live_selected_interpreter "$listener_pid" "listening process"
  done < <(
    { lsof -nP -iTCP -sTCP:LISTEN -Fp 2>/dev/null || true; } \
      | sed -n 's/^p//p' \
      | sort -u
  )
fi

echo "==> Installing Python dependencies"
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r requirements.txt
RUNTIME_INFO="$(veyra_runtime_info "$PYTHON_BIN" "$ROOT" "$PYTHON_SOURCE" "$VENV_PATH")"
echo "==> Dependency postflight passed: $RUNTIME_INFO"

echo "==> Initializing local state directory"
"$PYTHON_BIN" - "$SELECTED_STATE_ROOT" <<'PY'
import sys
from pathlib import Path

from core.world_state import WorldStateStore

store = WorldStateStore(Path(sys.argv[1]))
print(f"Initialized Veyra state at {store.root}")
PY

if [ ! -f .env ]; then
  echo "==> .env not found"
  echo "    Run: install -m 600 .env.example .env"
  echo "    Then fill local model/OpenClaw/Feishu values as needed."
fi

if command -v npm >/dev/null 2>&1; then
  echo "==> Installing and building the local console"
  mkdir -p "$ROOT/ui"
  UI_BUILD_TMP="$(mktemp -d "$ROOT/ui/.console-build.XXXXXX")"
  (cd web && npm install && npm run build -- --outDir "$UI_BUILD_TMP")
  if [ ! -f "$UI_BUILD_TMP/index.html" ]; then
    echo "Console build did not produce index.html; the existing console was left unchanged." >&2
    exit 1
  fi
  if [ -e "$ROOT/ui/console" ]; then
    UI_BACKUP="$ROOT/ui/.console-previous.$$"
    if [ -e "$UI_BACKUP" ]; then
      echo "Refusing to overwrite unexpected console backup path: $UI_BACKUP" >&2
      exit 1
    fi
    mv "$ROOT/ui/console" "$UI_BACKUP"
  fi
  if ! mv "$UI_BUILD_TMP" "$ROOT/ui/console"; then
    echo "Console swap failed; restoring the previous console." >&2
    if [ -n "$UI_BACKUP" ] && [ -d "$UI_BACKUP" ]; then
      if mv "$UI_BACKUP" "$ROOT/ui/console"; then
        UI_BACKUP=""
      else
        PRESERVE_UI_BACKUP=1
        echo "Previous console preserved for manual recovery at: $UI_BACKUP" >&2
      fi
    fi
    exit 1
  fi
  UI_BUILD_TMP=""
  [ -z "$UI_BACKUP" ] || rm -rf "$UI_BACKUP"
  UI_BACKUP=""
else
  echo "==> npm is not installed; skipping console build"
  echo "    Install Node.js/npm and run: cd web && npm install && npm run build"
fi

veyra_release_update_lock
trap - EXIT
echo "==> Install complete"
echo "Next: VEYRA_PYTHON=\"$PYTHON_BIN\" ./scripts/start_local.sh --check-runtime"
echo "Then: VEYRA_PYTHON=\"$PYTHON_BIN\" ./scripts/start_local.sh --foreground"
