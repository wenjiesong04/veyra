from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TEST_RUNTIME = TemporaryDirectory(prefix="veyra-tool-proxy-guard-")
TEST_ROOT = Path(_TEST_RUNTIME.name)
TEST_STATE_ROOT = TEST_ROOT / "state"
TEST_AGENCY_ROOT = TEST_ROOT / "agency"
os.environ.setdefault("VEYRA_STATE_ROOT", str(TEST_STATE_ROOT))
os.environ.setdefault("VEYRA_AGENCY_ROOT", str(TEST_AGENCY_ROOT))

from main import app  # noqa: E402


client = TestClient(app)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=payload)
    expect(response.status_code < 400, f"POST {path}", response.text)
    return response.json()


def get_json(path: str) -> dict[str, Any]:
    response = client.get(path)
    expect(response.status_code < 400, f"GET {path}", response.text)
    return response.json()


def proposal(payload: dict[str, Any]) -> dict[str, Any]:
    return post_json("/actions/proposals", payload)


def main() -> int:
    print("Veyra Tool Proxy guard smoke")
    scratch = TEST_STATE_ROOT / "tool_proxy_guard.txt"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text("before\n", encoding="utf-8")
    env_file = TEST_ROOT / ".env"
    env_file.write_text("SECRET=do-not-read\n", encoding="utf-8")

    read_file = proposal(
        {
            "proposal_id": "guard_read_file",
            "agent": "guard-smoke",
            "action": {"type": "file_read", "path": str(scratch)},
            "risk_guess": "R1",
            "reversible": "yes",
            "reason": "Agent requests read file through Tool Proxy.",
        }
    )
    expect(read_file.get("status") == "ok", "file read allowed", read_file)
    expect(read_file.get("tool_trace", {}).get("trace_id"), "file read tool trace", read_file)
    expect(read_file.get("verification", {}).get("status") == "verified_success", "file read verifier", read_file)

    write_file = proposal(
        {
            "proposal_id": "guard_write_file",
            "agent": "guard-smoke",
            "action": {"type": "file_write", "path": str(scratch), "content": "after\n"},
            "risk_guess": "R2",
            "reversible": "yes",
            "reason": "Agent requests scoped file write through Tool Proxy.",
        }
    )
    expect(write_file.get("status") == "ok", "file write allowed with policy", write_file)
    expect(write_file.get("execution_result", {}).get("snapshot", {}).get("snapshot_id"), "file write snapshot", write_file)
    expect(write_file.get("tool_trace", {}).get("snapshot_id"), "file write trace snapshot", write_file)
    expect(write_file.get("verification", {}).get("status") == "verified_success", "file write verifier", write_file)

    rm_rf = proposal(
        {
            "proposal_id": "guard_rm_rf",
            "agent": "guard-smoke",
            "action": {"type": "shell_command", "command": ["rm", "-rf", str(TEST_ROOT / "target")]},
            "risk_guess": "R5",
            "reversible": "no",
            "reason": "Agent attempts forbidden recursive delete.",
        }
    )
    expect(rm_rf.get("status") == "blocked", "rm -rf blocked", rm_rf)
    expect(rm_rf.get("tool_trace", {}).get("trace_id"), "rm -rf trace", rm_rf)
    expect(rm_rf.get("verification", {}).get("status") == "verified_failed", "rm -rf verifier", rm_rf)

    env_read = proposal(
        {
            "proposal_id": "guard_env_read",
            "agent": "guard-smoke",
            "action": {"type": "file_read", "path": str(env_file)},
            "risk_guess": "R4",
            "reversible": "yes",
            "reason": "Agent requests reading .env.",
        }
    )
    expect(env_read.get("status") in {"blocked", "needs_confirmation"}, ".env read blocked or review", env_read)
    expect(env_read.get("tool_trace", {}).get("trace_id"), ".env read trace", env_read)
    expect(bool(env_read.get("verification")), ".env read verifier", env_read)

    restart = proposal(
        {
            "proposal_id": "guard_restart",
            "agent": "guard-smoke",
            "action": {"type": "shell_command", "command": ["restart", "veyra"]},
            "risk_guess": "R4",
            "reversible": "partial",
            "reason": "Agent requests service restart.",
        }
    )
    expect(restart.get("status") == "needs_confirmation", "restart requires human review", restart)
    expect(restart.get("review", {}).get("review_id"), "restart review created", restart)
    expect(restart.get("tool_trace", {}).get("trace_id"), "restart trace", restart)

    force_push = proposal(
        {
            "proposal_id": "guard_force_push",
            "agent": "guard-smoke",
            "action": {"type": "shell_command", "command": ["git", "push", "--force"]},
            "risk_guess": "R5",
            "reversible": "no",
            "reason": "Agent attempts force push.",
        }
    )
    expect(force_push.get("status") == "blocked", "git push --force blocked", force_push)
    expect(force_push.get("tool_trace", {}).get("trace_id"), "force push trace", force_push)

    tool_logs = get_json("/logs/tools")
    traces = [item for item in tool_logs.get("items", []) if item.get("trace_id")]
    expect(len(traces) >= 6, "all scenarios emitted tool traces", traces)
    audit = get_json("/audit/journal?limit=200")
    proposals = [
        item
        for item in audit.get("items", [])
        if str(item.get("event_id") or "").startswith("guard_") or str(item.get("trace_id") or "").startswith("guard_")
    ]
    expect(len(proposals) >= 6, "all scenarios entered audit", proposals)

    print("Tool Proxy guard smoke passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"not ok - {exc}", file=sys.stderr)
        raise SystemExit(1)
