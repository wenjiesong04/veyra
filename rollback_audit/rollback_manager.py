from __future__ import annotations

import difflib
from pathlib import Path
from shutil import copy2
from typing import Any
from uuid import uuid4

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class RollbackManager:
    def __init__(self, state_store: WorldStateStore, snapshot_root: str = "state/snapshots") -> None:
        self.state_store = state_store
        self.snapshot_root = Path(snapshot_root)
        self.snapshot_root.mkdir(parents=True, exist_ok=True)

    def snapshot_file(self, path: str, reason: str = "") -> dict[str, Any]:
        source = Path(path)
        snapshot_id = f"snap_{uuid4().hex[:12]}"
        if not source.exists() or not source.is_file():
            snapshot = {"snapshot_id": snapshot_id, "status": "missing", "source": str(source), "reason": reason, "created_at": utc_now_iso()}
        else:
            target = self.snapshot_root / f"{snapshot_id}_{source.name}"
            copy2(source, target)
            snapshot = {
                "snapshot_id": snapshot_id,
                "status": "created",
                "source": str(source),
                "snapshot": str(target),
                "reason": reason,
                "created_at": utc_now_iso(),
            }
        state = self.state_store.read_json("rollback_state.json") or {"snapshots": []}
        state.setdefault("snapshots", []).append(snapshot)
        self.state_store.write_json("rollback_state.json", state)
        self.state_store.append_jsonl("rollback_log.jsonl", {"action": "snapshot", "snapshot": snapshot})
        return snapshot

    def restore(self, snapshot_id: str) -> dict[str, Any]:
        snapshots = self.state_store.read_json("rollback_state.json").get("snapshots", [])
        snapshot = next((item for item in snapshots if item.get("snapshot_id") == snapshot_id), None)
        if not snapshot:
            raise KeyError(f"Snapshot not found: {snapshot_id}")
        if snapshot.get("status") != "created":
            result = {"status": "not_restorable", "snapshot": snapshot}
        else:
            copy2(str(snapshot["snapshot"]), str(snapshot["source"]))
            result = {"status": "restored", "snapshot_id": snapshot_id, "source": snapshot["source"], "restored_at": utc_now_iso()}
        self.state_store.append_jsonl("rollback_log.jsonl", {"action": "restore", "result": result})
        return result

    def diff(self, snapshot_id: str) -> dict[str, Any]:
        snapshots = self.state_store.read_json("rollback_state.json").get("snapshots", [])
        snapshot = next((item for item in snapshots if item.get("snapshot_id") == snapshot_id), None)
        if not snapshot:
            raise KeyError(f"Snapshot not found: {snapshot_id}")
        if snapshot.get("status") != "created":
            return {"status": "not_available", "reason": "snapshot is not restorable", "snapshot": snapshot}

        snapshot_path = Path(str(snapshot["snapshot"]))
        source_path = Path(str(snapshot["source"]))
        if not snapshot_path.exists():
            return {"status": "missing_snapshot", "snapshot": snapshot}
        if not source_path.exists():
            return {"status": "missing_source", "snapshot": snapshot}

        before = snapshot_path.read_text(encoding="utf-8", errors="replace").splitlines()
        after = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
        diff = "\n".join(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"snapshot:{snapshot_path.name}",
                tofile=str(source_path),
                lineterm="",
            )
        )
        return {
            "status": "ok",
            "snapshot_id": snapshot_id,
            "source": str(source_path),
            "changed": bool(diff),
            "diff": diff or "No changes between snapshot and current file.",
        }
