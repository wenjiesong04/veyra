from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import app  # noqa: E402


client = TestClient(app)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def post_json(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.post(path, json=payload or {})
    expect(response.status_code < 400, f"POST {path}", response.text)
    return response.json()


def get_json(path: str) -> dict[str, Any]:
    response = client.get(path)
    expect(response.status_code < 400, f"GET {path}", response.text)
    return response.json()


def send_message(text: str) -> dict[str, Any]:
    return post_json(
        "/events/message",
        {
            "text": text,
            "channel": "self-test",
            "user_id": "mvp-self-test",
            "session_id": "mvp-self-test",
        },
    )


def main() -> int:
    print("Veyra MVP self-test")

    runtime = get_json("/runtime")
    expect(bool(runtime.get("identity", {}).get("name")), "runtime identity", runtime)

    contract = get_json("/agent/contract")
    expect(contract.get("contract_version") == "veyra.agent_adapter.v1", "agent adapter contract", contract)

    direct = send_message("Veyra 是什么")
    expect(direct.get("route") == "direct_answer", "direct answer route", direct)
    expect(direct.get("status") == "success", "direct answer success", direct)

    probe = send_message("帮我看 18789 端口有没有被占用")
    expect(probe.get("route") == "probe", "probe route", probe)

    blocked = send_message("请执行 rm -rf /")
    expect(blocked.get("route") == "block", "R5 block route", blocked)
    expect(blocked.get("status") == "blocked", "R5 blocked status", blocked)

    review = post_json(
        "/actions/proposals",
        {
            "proposal_id": "selftest_review_echo",
            "agent": "self-test",
            "action": {"type": "shell_command", "command": ["echo", "veyra-approved"]},
            "risk_guess": "R4",
            "reversible": "yes",
            "reason": "self-test approved shell action",
        },
    )
    review_id = review.get("review", {}).get("review_id")
    expect(bool(review_id), "human review created", review)
    approved = post_json(f"/reviews/{review_id}/approve", {"reason": "mvp_self_test"})
    expect(approved.get("status") == "approved", "human review approved", approved)
    expect(approved.get("execution_result", {}).get("status") == "ok", "approved action executed", approved)

    tool = post_json("/tool-proxy/shell", {"command": ["echo", "veyra-tool-proxy"]})
    expect(tool.get("status") == "ok", "tool proxy shell", tool)

    browser = post_json("/tool-proxy/browser/open", {"url": "http://127.0.0.1:8000/console/"})
    expect(browser.get("status") == "not_configured", "tool proxy browser policy", browser)

    api = post_json("/tool-proxy/api/request", {"payload": {"method": "GET", "url": "http://127.0.0.1:8000/state"}})
    expect(api.get("status") == "not_configured", "tool proxy api policy", api)

    policy_logs = get_json("/logs/policy")
    expect(bool(policy_logs.get("items")), "policy trace log", policy_logs)

    scratch = ROOT / "state" / "mvp_self_test.txt"
    scratch.write_text("before\n", encoding="utf-8")
    snapshot = post_json("/rollback/snapshot", {"path": str(scratch)})
    expect(snapshot.get("status") == "created", "rollback snapshot", snapshot)
    scratch.write_text("after\n", encoding="utf-8")
    diff = get_json(f"/rollback/{snapshot['snapshot_id']}/diff")
    expect(diff.get("changed") is True, "rollback diff", diff)
    restored = post_json(f"/rollback/{snapshot['snapshot_id']}/restore")
    expect(restored.get("status") == "restored", "rollback restore", restored)
    expect(scratch.read_text(encoding="utf-8") == "before\n", "rollback restored content")

    memory = post_json(
        "/memory/patch",
        {
            "patch": {
                "session_id": "mvp-self-test",
                "task": "MVP self-test memory write",
                "executor": "self-test",
                "status": "success",
                "result": {"checked": True},
            }
        },
    )
    expect(memory.get("status") == "written", "memory bridge write", memory)

    proactive = post_json("/proactive/check")
    expect(proactive.get("status") in {"success", "ok"}, "proactive check", proactive)

    mvp = get_json("/mvp/status")
    loops = mvp.get("core_loops", {})
    expect(all(loops.values()), "MVP readiness flags", loops)

    print("MVP self-test passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"not ok - {exc}", file=sys.stderr)
        raise SystemExit(1)
