#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/reset_local_state.sh [--force]

Backs up local runtime state, creates a fresh state directory, and removes the
ignored agency intention queue. Refuses to run while the local API is reachable
unless --force is supplied.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

HOST="${VEYRA_HOST:-127.0.0.1}"
PORT="${VEYRA_PORT:-8000}"

if python - "$HOST" "$PORT" <<'PY'
import sys
from urllib.request import urlopen

host, port = sys.argv[1], sys.argv[2]
try:
    with urlopen(f"http://{host}:{port}/health", timeout=1.5):
        raise SystemExit(0)
except Exception:
    raise SystemExit(1)
PY
then
  if [ "$FORCE" != "1" ]; then
    echo "Veyra API is reachable at http://$HOST:$PORT."
    echo "Stop it first, or rerun with --force if you intentionally want to reset local state."
    exit 1
  fi
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT=".veyra-local-backups"
mkdir -p "$BACKUP_ROOT"

if [ -d state ]; then
  BACKUP_PATH="$BACKUP_ROOT/state-$STAMP"
  echo "==> Backing up state/ to $BACKUP_PATH"
  mv state "$BACKUP_PATH"
fi

if [ -f agency/intention_queue.json ]; then
  BACKUP_PATH="$BACKUP_ROOT/intention_queue-$STAMP.json"
  echo "==> Backing up agency/intention_queue.json to $BACKUP_PATH"
  mv agency/intention_queue.json "$BACKUP_PATH"
fi

echo "==> Creating fresh local state"
python - <<'PY'
from core.world_state import WorldStateStore

store = WorldStateStore()
print(f"Initialized fresh Veyra state at {store.root}")
PY

echo "==> Reset complete"
