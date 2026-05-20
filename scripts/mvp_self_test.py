from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import app  # noqa: E402
from core.verifier import Verifier  # noqa: E402
from interface.agent_contract import normalize_capabilities  # noqa: E402
from interface.agent_adapter import ExecutionResult  # noqa: E402


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
    expect(
        contract.get("compatibility", {}).get("policy_version") == "veyra.agent_compatibility.v1",
        "agent compatibility policy",
        contract,
    )
    future_agent = normalize_capabilities(
        {
            "runtime": "custom",
            "status": "available",
            "contract_version": "veyra.agent_adapter.v2",
            "features": {"rendered_prompt_fallback": True},
        },
        runtime="custom",
    )
    expect(future_agent.get("compatibility", {}).get("status") == "unverified", "future agent contract fallback", future_agent)
    incompatible_agent = normalize_capabilities(
        {
            "runtime": "custom",
            "status": "available",
            "features": {"rendered_prompt_fallback": False},
        },
        runtime="custom",
    )
    expect(incompatible_agent.get("compatibility", {}).get("status") == "incompatible", "agent compatibility block", incompatible_agent)

    direct = send_message("Veyra 是什么")
    expect(direct.get("route") == "direct_answer", "direct answer route", direct)
    expect(direct.get("status") == "success", "direct answer success", direct)

    probe = send_message("帮我看 18789 端口有没有被占用")
    expect(probe.get("route") == "probe", "probe route", probe)
    expect(probe.get("artifacts", {}).get("execution_trace", {}).get("trace_id"), "probe execution trace", probe)

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
    expect(bool(tool.get("tool_trace", {}).get("trace_id")), "tool proxy shell trace", tool)

    browser = post_json("/tool-proxy/browser/open", {"url": "http://127.0.0.1:8000/console/"})
    expect(browser.get("status") == "not_configured", "tool proxy browser policy", browser)
    expect(browser.get("tool_trace", {}).get("action_type") == "browser_open", "tool proxy browser trace", browser)

    api = post_json("/tool-proxy/api/request", {"payload": {"method": "GET", "url": "http://127.0.0.1:8000/state"}})
    expect(api.get("status") == "not_configured", "tool proxy api policy", api)
    expect(api.get("tool_trace", {}).get("action_type") == "api_request", "tool proxy api trace", api)

    file_target = ROOT / "state" / "mvp_self_test_tool_file.txt"
    file_target.write_text("file-before\n", encoding="utf-8")
    file_write = post_json("/tool-proxy/file/write", {"path": str(file_target), "content": "file-after\n", "reason": "mvp_self_test"})
    expect(file_write.get("status") == "ok", "tool proxy file write", file_write)
    expect(file_write.get("snapshot", {}).get("checksum"), "tool proxy file snapshot checksum", file_write)
    expect(file_write.get("tool_trace", {}).get("snapshot_id") == file_write.get("snapshot", {}).get("snapshot_id"), "tool proxy file trace snapshot", file_write)

    policy_logs = get_json("/logs/policy")
    expect(bool(policy_logs.get("items")), "policy trace log", policy_logs)
    tool_logs = get_json("/logs/tools")
    standard_tools = [item for item in tool_logs.get("items", []) if item.get("trace_id")]
    expect(len(standard_tools) >= 5, "standard tool trace ids", standard_tools[-5:])
    execution_logs = get_json("/logs/execution")
    expect(bool(execution_logs.get("items")), "execution trace log", execution_logs)

    scratch = ROOT / "state" / "mvp_self_test.txt"
    scratch.write_text("before\n", encoding="utf-8")
    snapshot = post_json("/rollback/snapshot", {"path": str(scratch)})
    expect(snapshot.get("status") == "created", "rollback snapshot", snapshot)
    scratch.write_text("after\n", encoding="utf-8")
    diff = get_json(f"/rollback/{snapshot['snapshot_id']}/diff")
    expect(diff.get("changed") is True, "rollback diff", diff)
    expect(diff.get("source_exists") is True and diff.get("snapshot_exists") is True, "rollback diff evidence", diff)
    restored = post_json(f"/rollback/{snapshot['snapshot_id']}/restore")
    expect(restored.get("status") == "restored", "rollback restore", restored)
    expect(restored.get("source_checksum") == restored.get("snapshot_checksum"), "rollback restore checksum", restored)
    expect(scratch.read_text(encoding="utf-8") == "before\n", "rollback restored content")

    verifier = Verifier()
    verified_success = verifier.verify_execution_result(
        ExecutionResult(task_id="v_success", executor="self-test", status="success", result="done", raw={"evidence": True})
    )
    expect(verified_success.get("status") == "verified_success", "verifier success evidence", verified_success)
    submitted = verifier.verify_execution_result(
        ExecutionResult(task_id="v_submitted", executor="self-test", status="submitted", result="queued")
    )
    expect(submitted.get("status") == "partially_success", "verifier submitted status", submitted)
    unconfigured = verifier.verify_execution_result(
        ExecutionResult(task_id="v_agent", executor="openclaw", status="adapter_unconfigured", result="")
    )
    expect(unconfigured.get("status") == "needs_more_probe", "verifier adapter unconfigured", unconfigured)
    failed_with_changes = verifier.verify_execution_result(
        ExecutionResult(task_id="v_failed", executor="self-test", status="failed", result="failed", changed_files=["state/mvp_self_test.txt"])
    )
    expect(failed_with_changes.get("status") == "needs_rollback", "verifier rollback needed", failed_with_changes)

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
