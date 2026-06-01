#!/usr/bin/env python3
"""One-shot cleanup for bloated local state/agency storage."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.state_compact import (  # noqa: E402
    compact_active_loop_tick,
    compact_intention,
    compact_review_item,
)
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import utc_now_iso  # noqa: E402
from runtime.retention_policy import RetentionPolicy  # noqa: E402


TERMINAL_INTENTION_STATUSES = {"done", "blocked", "dismissed", "executed_read_only"}
TEST_CHANNEL_PREFIXES = (
    "live-verify",
    "live-check",
    "manual-",
    "smoke",
    "self-test",
    "verify",
    "p6",
    "cmt-",
    "accept-",
    "diagnostic-",
)
MALICIOUS_REVIEW_MARKERS = ("钓鱼", "phishing", "rm -rf")


def selected_state_root() -> Path:
    explicit = os.getenv("VEYRA_STATE_DIR") or os.getenv("VEYRA_STATE_ROOT")
    if explicit:
        return Path(explicit)
    env = os.getenv("VEYRA_ENV", "").strip().lower()
    if env in {"dev", "prod", "test"}:
        return ROOT / "state" / env
    return ROOT / "state"


def selected_agency_root() -> Path:
    explicit = os.getenv("VEYRA_AGENCY_DIR") or os.getenv("VEYRA_AGENCY_ROOT")
    if explicit:
        return Path(explicit)
    env = os.getenv("VEYRA_ENV", "").strip().lower()
    if env in {"dev", "prod", "test"}:
        return ROOT / "agency" / env
    return ROOT / "agency"


def file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def cleanup_logs(state_root: Path, *, dry_run: bool) -> dict[str, Any]:
    store = WorldStateStore(state_root)
    policy = RetentionPolicy(store)
    summary_before = policy.summary()
    over_before = [item for item in summary_before.get("files", []) if item.get("status") == "over_limit"]
    if dry_run:
        return {"status": "dry_run", "over_limit_before": over_before}
    enforced = policy.enforce()
    return {
        "status": "success",
        "changed": enforced.get("changed", 0),
        "files": enforced.get("files", []),
        "over_limit_before": len(over_before),
    }


def cleanup_intentions(agency_root: Path, *, dry_run: bool, keep_terminal: int = 30) -> dict[str, Any]:
    path = agency_root / "intention_queue.json"
    if not path.exists():
        return {"status": "skipped", "reason": "missing intention_queue.json"}
    before = file_size(path)
    items = json.loads(path.read_text(encoding="utf-8") or "[]")
    if not isinstance(items, list):
        items = []

    active: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        compact = compact_intention(item)
        if status in TERMINAL_INTENTION_STATUSES:
            terminal.append(compact)
        else:
            active.append(compact)

    kept_terminal = terminal[-keep_terminal:]
    cleaned = active + kept_terminal
    after_count = len(cleaned)
    if not dry_run:
        path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    after = file_size(path) if not dry_run else before
    return {
        "status": "dry_run" if dry_run else "success",
        "before_bytes": before,
        "after_bytes": after,
        "before_count": len(items),
        "after_count": after_count,
        "removed_terminal": max(0, len(terminal) - keep_terminal),
    }


def cleanup_review_queue(state_root: Path, *, dry_run: bool, keep_terminal: int = 40) -> dict[str, Any]:
    store = WorldStateStore(state_root)
    path = store.path_for("review_queue.json")
    before = file_size(path)
    state = store.read_json("review_queue.json") or {"items": []}
    items = state.get("items") if isinstance(state.get("items"), list) else []

    pending: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    auto_rejected = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        task_text = str(item.get("task_text") or "")
        status = str(item.get("status") or "")
        compact = compact_review_item(item)
        if status == "pending" and any(marker in task_text.lower() for marker in MALICIOUS_REVIEW_MARKERS):
            compact["status"] = "rejected"
            compact["decided_at"] = utc_now_iso()
            compact["decision_reason"] = "storage_cleanup:auto_reject_malicious_pending_review"
            auto_rejected += 1
            terminal.append(compact)
            continue
        if status == "pending":
            pending.append(compact)
        else:
            terminal.append(compact)

    cleaned_items = pending + terminal[-keep_terminal:]
    if not dry_run:
        store.write_json("review_queue.json", {"items": cleaned_items})
    after = file_size(path) if not dry_run else before
    return {
        "status": "dry_run" if dry_run else "success",
        "before_bytes": before,
        "after_bytes": after,
        "before_count": len(items),
        "after_count": len(cleaned_items),
        "auto_rejected": auto_rejected,
        "removed_terminal": max(0, len(terminal) - keep_terminal),
    }


def cleanup_channel_state(state_root: Path, *, dry_run: bool, inbox_limit: int = 200, outbox_limit: int = 200, seen_limit: int = 300) -> dict[str, Any]:
    store = WorldStateStore(state_root)
    path = store.path_for("channel_state.json")
    before = file_size(path)
    state = store.read_json("channel_state.json") or {}
    if not isinstance(state, dict):
        state = {}

    inbox = state.get("inbox") if isinstance(state.get("inbox"), list) else []
    outbox = state.get("outbox") if isinstance(state.get("outbox"), list) else []
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    seen = state.get("seen_message_ids") if isinstance(state.get("seen_message_ids"), dict) else {}

    pruned_sessions = {}
    removed_sessions = 0
    for session_id, payload in sessions.items():
        channel = str((payload or {}).get("channel") or "")
        if any(channel.startswith(prefix) or prefix in session_id for prefix in TEST_CHANNEL_PREFIXES):
            removed_sessions += 1
            continue
        pruned_sessions[session_id] = payload

    seen_items = list(seen.items())
    if len(seen_items) > seen_limit:
        seen_items = seen_items[-seen_limit:]

    state["inbox"] = inbox[-inbox_limit:]
    state["outbox"] = outbox[-outbox_limit:]
    state["sessions"] = pruned_sessions
    state["seen_message_ids"] = dict(seen_items)

    if not dry_run:
        store.write_json("channel_state.json", state)
    after = file_size(path) if not dry_run else before
    return {
        "status": "dry_run" if dry_run else "success",
        "before_bytes": before,
        "after_bytes": after,
        "inbox_before": len(inbox),
        "inbox_after": len(state["inbox"]),
        "outbox_before": len(outbox),
        "outbox_after": len(state["outbox"]),
        "sessions_before": len(sessions),
        "sessions_after": len(pruned_sessions),
        "seen_before": len(seen),
        "seen_after": len(state["seen_message_ids"]),
        "removed_test_sessions": removed_sessions,
    }


def cleanup_active_loop(state_root: Path, *, dry_run: bool, tick_limit: int = 12) -> dict[str, Any]:
    store = WorldStateStore(state_root)
    path = store.path_for("active_loop_state.json")
    before = file_size(path)
    state = store.read_json("active_loop_state.json") or {}
    ticks = state.get("ticks") if isinstance(state.get("ticks"), list) else []
    compact_ticks = [compact_active_loop_tick(tick) for tick in ticks[-tick_limit:] if isinstance(tick, dict)]
    last_tick = compact_ticks[-1] if compact_ticks else state.get("last_tick")
    if isinstance(last_tick, dict):
        last_tick = compact_active_loop_tick(last_tick)
    state["ticks"] = compact_ticks
    state["last_tick"] = last_tick
    if not dry_run:
        store.write_json("active_loop_state.json", state)
    after = file_size(path) if not dry_run else before
    return {
        "status": "dry_run" if dry_run else "success",
        "before_bytes": before,
        "after_bytes": after,
        "ticks_before": len(ticks),
        "ticks_after": len(compact_ticks),
    }


def cleanup_task_state(state_root: Path, *, dry_run: bool, history_limit: int = 80) -> dict[str, Any]:
    store = WorldStateStore(state_root)
    path = store.path_for("task_state.json")
    before = file_size(path)
    state = store.read_json("task_state.json") or {}
    history = state.get("history") if isinstance(state.get("history"), list) else []
    short_term = state.get("short_term_memory") if isinstance(state.get("short_term_memory"), list) else []
    state["history"] = history[-history_limit:]
    state["short_term_memory"] = short_term[-40:]
    pending = state.get("pending_agent_tasks")
    if isinstance(pending, list) and len(pending) > 20:
        state["pending_agent_tasks"] = pending[-20:]
    if not dry_run:
        store.write_json("task_state.json", state)
    after = file_size(path) if not dry_run else before
    return {
        "status": "dry_run" if dry_run else "success",
        "before_bytes": before,
        "after_bytes": after,
        "history_before": len(history),
        "history_after": len(state["history"]),
        "short_term_before": len(short_term),
        "short_term_after": len(state["short_term_memory"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean up bloated Veyra local storage.")
    parser.add_argument("--state-root", default=str(selected_state_root()))
    parser.add_argument("--agency-root", default=str(selected_agency_root()))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state_root = Path(args.state_root)
    agency_root = Path(args.agency_root)
    report = {
        "schema": "veyra.state_storage_cleanup.v1",
        "dry_run": args.dry_run,
        "state_root": str(state_root),
        "agency_root": str(agency_root),
        "sections": {},
    }

    report["sections"]["logs"] = cleanup_logs(state_root, dry_run=args.dry_run)
    report["sections"]["intention_queue"] = cleanup_intentions(agency_root, dry_run=args.dry_run)
    report["sections"]["review_queue"] = cleanup_review_queue(state_root, dry_run=args.dry_run)
    report["sections"]["channel_state"] = cleanup_channel_state(state_root, dry_run=args.dry_run)
    report["sections"]["active_loop_state"] = cleanup_active_loop(state_root, dry_run=args.dry_run)
    report["sections"]["task_state"] = cleanup_task_state(state_root, dry_run=args.dry_run)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
