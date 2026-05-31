#!/usr/bin/env python3
"""Runtime acceptance for User Commitment + Proactive Push.

Usage:
  # Against a running API (recommended for real acceptance):
  uvicorn main:app --host 127.0.0.1 --port 8000
  python scripts/commitment_runtime_acceptance.py http://127.0.0.1:8000

  # Embedded TestClient (CI / no server):
  python scripts/commitment_runtime_acceptance.py --embedded
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.event_schema import utc_now_iso  # noqa: E402

Report = dict[str, Any]


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")


def past_iso(seconds: int = 30) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def future_iso(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class HttpClient:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 60.0) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AssertionError(f"{method} {path} HTTP {exc.code}: {detail[:500]}") from exc

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("POST", path, body or {})


def build_embedded_client() -> tuple[Any, Path, Callable[[], Any]]:
    from fastapi.testclient import TestClient

    tmp = TemporaryDirectory(prefix="veyra-commitment-accept-")
    state_root = Path(tmp.name) / "state"
    agency_root = Path(tmp.name) / "agency"
    os.environ["VEYRA_STATE_ROOT"] = str(state_root)
    os.environ["VEYRA_AGENCY_ROOT"] = str(agency_root)
    agency_root.mkdir(parents=True, exist_ok=True)
    (agency_root / "goals.json").write_text("{}", encoding="utf-8")
    (agency_root / "intention_queue.json").write_text("[]", encoding="utf-8")

    from main import app  # noqa: E402

    client = TestClient(app)

    class Wrapper:
        def get(self, path: str) -> dict[str, Any]:
            response = client.get(path)
            expect(response.status_code < 400, f"GET {path}", response.text)
            return response.json()

        def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            response = client.post(path, json=body or {})
            expect(response.status_code < 400, f"POST {path}", response.text)
            return response.json()

    return Wrapper(), state_root, lambda: app


def read_action_routes(state_root: Path | None, client: Any, routes: set[str]) -> list[dict[str, Any]]:
    if state_root is not None:
        path = state_root / "logs" / "action_record.jsonl"
        if not path.exists():
            return []
        items = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("route") in routes:
                items.append(row)
        return items
    logs = client.get("/logs/actions?limit=200")
    items = logs.get("items") if isinstance(logs.get("items"), list) else []
    return [row for row in items if isinstance(row, dict) and row.get("route") in routes]


def outbox_for_commitment(client: Any, commitment_id: str) -> list[dict[str, Any]]:
    payload = client.get("/channels/outbox?limit=200")
    items = payload.get("items") if isinstance(payload.get("items"), list) else payload.get("outbox", [])
    if not isinstance(items, list):
        return []
    matched = []
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata.get("commitment_id") == commitment_id:
            matched.append(item)
    return matched


def run_happy_path(client: Any, state_root: Path | None, report: Report) -> str:
    created = client.post(
        "/commitments",
        {
            "kind": "weather_daily",
            "title": "验收：北京天气",
            "user_id": "accept-user",
            "channel": "api",
            "session_id": "accept-session",
            "status": "active",
            "next_run_at": past_iso(5),
            "schedule": {"kind": "daily", "time_local": "08:00", "timezone": "Asia/Shanghai"},
            "payload": {"location": "北京", "topic": "weather"},
        },
    )
    commitment = created["commitment"]
    commitment_id = commitment["commitment_id"]
    expect(commitment.get("confirmed_at"), "active commitment is confirmed", commitment)

    due_before = client.post("/commitments/run-due", {"limit": 5, "reason": "acceptance_first"})
    expect(due_before.get("due_count", 0) >= 1, "due commitment recognized", due_before)
    processed = due_before.get("processed") if isinstance(due_before.get("processed"), list) else []
    first = next((row for row in processed if row.get("commitment_id") == commitment_id), None)
    expect(first is not None, "due commitment processed", processed)
    expect(first.get("guardian") in {"allow", "allow_with_constraints"}, "guardian decision recorded", first)
    expect(
        first.get("status") in {"queued", "sent", "not_configured"},
        "delivery reaches channel adapter or outbox",
        first,
    )
    if first.get("weather_probe_status"):
        expect(
            first["weather_probe_status"] not in {"fabricated"},
            "weather_probe does not report fabricated status",
            first["weather_probe_status"],
        )

    outbox = outbox_for_commitment(client, commitment_id)
    expect(len(outbox) >= 1, "outbox records commitment push", outbox[:1])

    refreshed = client.get(f"/commitments/{commitment_id}")
    updated = refreshed["commitment"]
    history = updated.get("push_history") if isinstance(updated.get("push_history"), list) else []
    expect(len(history) >= 1, "push_history appended", history)
    expect(updated.get("last_run_at"), "last_run_at updated", updated)
    next_run = updated.get("next_run_at")
    expect(next_run and next_run > past_iso(), "next_run_at advanced", {"before": past_iso(), "after": next_run})

    audit = read_action_routes(state_root, client, {"commitment_push_due", "commitment_push_attempt"})
    expect(any(row.get("route") == "commitment_push_due" for row in audit), "commitment_push_due audit exists", audit[-3:])
    expect(any(row.get("route") == "commitment_push_attempt" for row in audit), "commitment_push_attempt audit exists", audit[-5:])

    loop_start = client.post("/runtime/active-loop/start", {"interval_seconds": 300})
    expect(loop_start.get("status") in {"running", "already_running"}, "active loop start", loop_start)
    tick = client.post("/runtime/active-loop/tick", {"reason": "acceptance", "include_runtime_matrix": False})
    steps = tick.get("steps") if isinstance(tick.get("steps"), list) else []
    step_names = [step.get("name") for step in steps if isinstance(step, dict)]
    expect("commitment_push" in step_names, "active loop tick runs commitment_push", step_names)
    try:
        stop = client.post("/runtime/active-loop/stop")
        if stop.get("status") not in {"stopped", "stale", "stopping"}:
            print(f"warn - active-loop stop returned {stop}")
    except AssertionError as exc:
        print(f"warn - active-loop stop failed (non-fatal for acceptance): {exc}")

    report["happy_path"] = {"commitment_id": commitment_id, "first_push": first, "outbox_count": len(outbox)}
    return commitment_id


def run_security_checks(client: Any, state_root: Path | None, report: Report) -> None:
    pending = client.post(
        "/commitments",
        {
            "kind": "weather_daily",
            "status": "pending_confirmation",
            "user_id": "accept-user",
            "channel": "api",
            "session_id": "accept-pending",
            "next_run_at": past_iso(5),
            "payload": {"location": "北京"},
        },
    )
    pending_id = pending["commitment"]["commitment_id"]
    pending_due = client.post("/commitments/run-due", {"limit": 10, "reason": "acceptance_pending"})
    processed = pending_due.get("processed") if isinstance(pending_due.get("processed"), list) else []
    expect(not any(row.get("commitment_id") == pending_id and row.get("status") in {"queued", "sent"} for row in processed), "pending not delivered", processed)
    pending_state = client.get(f"/commitments/{pending_id}")["commitment"]
    expect(len(pending_state.get("push_history") or []) == 0, "pending has no push_history", pending_state)

    paused = client.post(
        "/commitments",
        {
            "kind": "generic_reminder",
            "status": "active",
            "user_id": "accept-user",
            "channel": "api",
            "session_id": "accept-paused",
            "next_run_at": past_iso(5),
            "payload": {"note": "paused test"},
        },
    )
    paused_id = paused["commitment"]["commitment_id"]
    client.post(f"/commitments/{paused_id}/pause")
    paused_due = client.post("/commitments/run-due", {"limit": 10, "reason": "acceptance_paused"})
    processed = paused_due.get("processed") if isinstance(paused_due.get("processed"), list) else []
    expect(not any(row.get("commitment_id") == paused_id for row in processed), "paused not in due processed", processed)

    cancelled = client.post(
        "/commitments",
        {
            "kind": "generic_reminder",
            "status": "active",
            "user_id": "accept-user",
            "channel": "api",
            "session_id": "accept-cancel",
            "next_run_at": past_iso(5),
            "payload": {"note": "cancel test"},
        },
    )
    cancel_id = cancelled["commitment"]["commitment_id"]
    client.post(f"/commitments/{cancel_id}/cancel")
    cancel_due = client.post("/commitments/run-due", {"limit": 10, "reason": "acceptance_cancel"})
    processed = cancel_due.get("processed") if isinstance(cancel_due.get("processed"), list) else []
    expect(not any(row.get("commitment_id") == cancel_id for row in processed), "cancelled not processed", processed)

    dup = client.post(
        "/commitments",
        {
            "kind": "generic_reminder",
            "status": "active",
            "user_id": "accept-user",
            "channel": "api",
            "session_id": "accept-dup",
            "next_run_at": past_iso(5),
            "payload": {"note": "dup test"},
        },
    )
    dup_id = dup["commitment"]["commitment_id"]
    first = client.post("/commitments/run-due", {"limit": 5, "reason": "acceptance_dup_1"})
    first_row = next((row for row in first.get("processed", []) if row.get("commitment_id") == dup_id), {})
    expect(first_row.get("status") in {"queued", "sent", "not_configured"}, "first duplicate-window push delivered", first_row)
    second = client.post("/commitments/run-due", {"limit": 5, "reason": "acceptance_dup_2"})
    second_row = next((row for row in second.get("processed", []) if row.get("commitment_id") == dup_id), None)
    if second_row:
        expect(second_row.get("status") == "skipped", "rapid second push skipped", second_row)
        expect(second_row.get("reason") == "push_cooldown", "cooldown reason", second_row)
    else:
        expect(second.get("due_count", 0) == 0, "second run has no due duplicate within cooldown", second)

    report["security"] = {
        "pending_id": pending_id,
        "paused_id": paused_id,
        "cancel_id": cancel_id,
        "dup_id": dup_id,
    }


def run_guardian_block_embedded(_app_factory: Callable[[], Any], _state_root: Path, report: Report) -> None:
    from core.guardian_controller import GuardianController

    original = GuardianController.review_text_action

    def blocked_review(self, text: str, decision, foresight):  # type: ignore[no-untyped-def]
        return {
            "decision": "block",
            "risk_level": decision.risk_level.value,
            "reason": "acceptance forced block",
            "policy": {},
            "foresight": foresight,
            "required_preconditions": [],
            "forbidden": [],
            "message_to_executor": "blocked for acceptance",
        }

    GuardianController.review_text_action = blocked_review  # type: ignore[method-assign]
    try:
        client, _, _ = build_embedded_client()
        blocked = client.post(
            "/commitments",
            {
                "kind": "generic_reminder",
                "status": "active",
                "user_id": "accept-user",
                "channel": "api",
                "session_id": "accept-block",
                "next_run_at": past_iso(5),
                "payload": {"note": "guardian block"},
            },
        )
        block_id = blocked["commitment"]["commitment_id"]
        before = client.get(f"/commitments/{block_id}")["commitment"]
        next_before = before.get("next_run_at")
        result = client.post("/commitments/run-due", {"limit": 5, "reason": "acceptance_guardian"})
        row = next((item for item in result.get("processed", []) if item.get("commitment_id") == block_id), {})
        expect(row.get("status") == "blocked", "guardian blocks push", row)
        expect(not outbox_for_commitment(client, block_id), "blocked push not in outbox", row)
        after = client.get(f"/commitments/{block_id}")["commitment"]
        expect(after.get("next_run_at") == next_before, "blocked push does not advance next_run_at", after)
    finally:
        GuardianController.review_text_action = original  # type: ignore[method-assign]
    report["guardian_block"] = {"status": "checked_embedded"}


def probe_live(base: str) -> bool:
    try:
        urllib.request.urlopen(f"{base.rstrip('/')}/runtime", timeout=3)
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Commitment runtime acceptance")
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:8000")
    parser.add_argument("--embedded", action="store_true", help="Use in-process TestClient instead of HTTP")
    args = parser.parse_args()

    report: Report = {"schema": "veyra.commitment_runtime_acceptance.v1", "started_at": utc_now_iso()}

    if args.embedded:
        client, state_root, app_factory = build_embedded_client()
        report["mode"] = "embedded"
        run_happy_path(client, state_root, report)
        run_security_checks(client, state_root, report)
        run_guardian_block_embedded(app_factory, state_root, report)
    else:
        base = args.base_url
        if not probe_live(base):
            print(f"Cannot reach {base}/runtime — start API first, or pass --embedded", file=sys.stderr)
            return 1
        client = HttpClient(base)
        report["mode"] = "live_http"
        report["base_url"] = base
        run_happy_path(client, None, report)
        run_security_checks(client, None, report)
        print("note: guardian block check runs only with --embedded")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("commitment runtime acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
