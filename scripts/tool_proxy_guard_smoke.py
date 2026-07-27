from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
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
os.environ["VEYRA_STATE_DIR"] = str(TEST_STATE_ROOT)
os.environ["VEYRA_STATE_ROOT"] = str(TEST_STATE_ROOT)
os.environ["VEYRA_AGENCY_DIR"] = str(TEST_AGENCY_ROOT)
os.environ["VEYRA_AGENCY_ROOT"] = str(TEST_AGENCY_ROOT)
os.environ["VEYRA_ENV_FILE"] = str(TEST_ROOT / "missing.env")
os.environ["VEYRA_CORE_MODEL_ENABLED"] = "0"
os.environ["VEYRA_ACTIVE_LOOP_AUTOSTART"] = "0"
os.environ["VEYRA_FEISHU_WS_AUTOSTART"] = "0"

from core.understanding_core import TurnUnderstanding  # noqa: E402
from main import app, awareness_loop  # noqa: E402


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


def semantic_understanding(
    text: str,
    *,
    resolver_status: str,
    operation: str,
    target_type: str,
    target_value: str,
) -> TurnUnderstanding:
    return TurnUnderstanding.from_payload(
        {
            "intent": "action",
            "task_type": "workspace_task",
            "explicit_request": text,
            "user_goal": text,
            "suggested_mode": "governed_execution",
            "semantic_frame": {
                "schema_version": "veyra.semantic_frame.v1",
                "acts": [
                    {
                        "act_id": "guard-a1",
                        "kind": "workspace_task",
                        "goal": text,
                        "operation": operation,
                        "target": {
                            "type": target_type,
                            "value": target_value,
                            "attributes": {},
                        },
                        "polarity": "positive",
                        "explicitness": "explicit",
                        "source_quote": {
                            "text": text,
                            "start": 0,
                            "end": len(text),
                        },
                        "speaker": "guard-smoke-user",
                        "authority": "direct_user",
                        "mention_mode": "normal_use",
                        "evidence_need": "fresh_local",
                        "referent": {
                            "surface": target_value,
                            "resolved": target_value,
                            "status": "resolved",
                            "candidates": [],
                        },
                        "condition": None,
                        "modality": "asserted",
                        "arguments": {},
                    }
                ],
                "relations": [],
                "ambiguities": [],
                "resolver_status": resolver_status,
                "source": "guard_smoke_fixture",
            },
        },
        source_text=text,
    )


@contextmanager
def injected_understanding(understanding: TurnUnderstanding) -> Iterator[None]:
    original_build = awareness_loop.understanding_core.build
    awareness_loop.understanding_core.build = lambda **_kwargs: understanding
    try:
        yield
    finally:
        awareness_loop.understanding_core.build = original_build


def decision_artifacts(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    artifacts = (
        payload.get("artifacts")
        if isinstance(payload.get("artifacts"), dict)
        else {}
    )
    decision = (
        artifacts.get("decision")
        if isinstance(artifacts.get("decision"), dict)
        else {}
    )
    if not decision:
        guardian = (
            artifacts.get("guardian")
            if isinstance(artifacts.get("guardian"), dict)
            else {}
        )
        decision = (
            guardian.get("decision_trace")
            if isinstance(guardian.get("decision_trace"), dict)
            else {}
        )
    assist = (
        decision.get("model_assist")
        if isinstance(decision.get("model_assist"), dict)
        else {}
    )
    policy = (
        assist.get("semantic_policy")
        if isinstance(assist.get("semantic_policy"), dict)
        else {}
    )
    return artifacts, decision, policy


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
    expect(
        read_file.get("verification", {}).get("status") == "needs_more_probe"
        and read_file.get("verification", {}).get("verdict")
        == "tool_proxy_trace_missing",
        "legacy file read trace is not upgraded without an authoritative Grant receipt",
        read_file,
    )

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
    expect(
        write_file.get("verification", {}).get("status") == "needs_more_probe"
        and write_file.get("verification", {}).get("verdict")
        == "tool_proxy_trace_missing",
        "legacy file write trace is not upgraded without an authoritative Grant receipt",
        write_file,
    )

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

    launchctl_restart = proposal(
        {
            "proposal_id": "guard_launchctl_restart",
            "agent": "guard-smoke",
            "action": {"type": "shell_command", "command": ["launchctl", "kickstart", "-k", "gui/501/ai.veyra.api"]},
            "risk_guess": "R2",
            "reversible": "partial",
            "reason": "Agent requests service restart through launchctl.",
        }
    )
    expect(launchctl_restart.get("status") == "needs_confirmation", "launchctl kickstart requires human review", launchctl_restart)
    expect(launchctl_restart.get("risk_level") == "R4", "launchctl kickstart risk raised to R4", launchctl_restart)
    expect(
        launchctl_restart.get("risk_assessment", {}).get("category") == "service_control",
        "launchctl kickstart classified as service control",
        launchctl_restart,
    )
    expect(launchctl_restart.get("review", {}).get("review_id"), "launchctl restart review created", launchctl_restart)

    systemctl_restart = proposal(
        {
            "proposal_id": "guard_systemctl_restart",
            "agent": "guard-smoke",
            "action": {"type": "shell_command", "command": ["systemctl", "restart", "nginx"]},
            "risk_guess": "R1",
            "reversible": "partial",
            "reason": "Agent requests service restart through systemctl.",
        }
    )
    expect(systemctl_restart.get("status") == "needs_confirmation", "systemctl restart requires human review", systemctl_restart)
    expect(systemctl_restart.get("risk_level") == "R4", "systemctl restart risk raised to R4", systemctl_restart)

    kill_process = proposal(
        {
            "proposal_id": "guard_kill_process",
            "agent": "guard-smoke",
            "action": {"type": "shell_command", "command": ["kill", "-9", "123"]},
            "risk_guess": "R1",
            "reversible": "partial",
            "reason": "Agent requests process termination.",
        }
    )
    expect(kill_process.get("status") == "needs_confirmation", "process kill requires human review", kill_process)
    expect(kill_process.get("risk_level") == "R4", "process kill risk raised to R4", kill_process)

    direct_launchctl = post_json("/tool-proxy/shell", {"command": ["launchctl", "kickstart", "-k", "gui/501/ai.veyra.api"]})
    expect(direct_launchctl.get("status") == "needs_confirmation", "direct ToolProxy launchctl requires human review", direct_launchctl)
    expect(direct_launchctl.get("review", {}).get("review_id"), "direct ToolProxy launchctl review created", direct_launchctl)

    natural_restart_text = "帮我重启 Veyra 服务。"
    resolved_restart = semantic_understanding(
        natural_restart_text,
        resolver_status="resolved",
        operation="restart_service",
        target_type="service",
        target_value="Veyra",
    )
    with injected_understanding(resolved_restart):
        natural_restart = post_json(
            "/events/message",
            {
                "text": natural_restart_text,
                "channel": "api",
                "user_id": "guard-smoke-user",
                "session_id": "guard-smoke-session",
                "message_id": "guard-natural-restart-resolved",
            },
        )
    expect(natural_restart.get("route") == "human_review", "natural restart enters human review", natural_restart)
    expect(natural_restart.get("risk_level") == "R4", "natural restart keeps the deterministic R4 floor", natural_restart)
    artifacts, decision, policy = decision_artifacts(natural_restart)
    expect(
        "agent.execute" in (policy.get("allowed_effects") or [])
        and decision.get("route") == "human_review",
        "natural restart is understood first while the R4 review floor remains authoritative",
        natural_restart,
    )
    natural_review_id = str((artifacts.get("review") or {}).get("review_id") or "")
    expect(natural_review_id, "natural restart review created", natural_restart)
    expect(
        artifacts.get("proposal") is None
        and "不会自动执行" in str(natural_restart.get("response") or ""),
        "review without a proposal is described as governance-only",
        natural_restart,
    )
    approved_natural_review = post_json(
        f"/reviews/{natural_review_id}/approve",
        {"reason": "guard smoke validates governance-only approval"},
    )
    expect(
        approved_natural_review.get("execution_result", {}).get("status")
        == "approved_noop",
        "governance-only approval records an explicit no-op",
        approved_natural_review,
    )
    natural_event_id = str(natural_restart.get("event_id") or "")
    natural_action_records = [
        item
        for item in (get_json("/logs/actions?limit=200").get("items") or [])
        if item.get("event_id") == natural_event_id
        and item.get("route") == "human_review"
    ]
    expect(
        any(
            item.get("status") == "governance_only"
            for item in natural_action_records
        )
        and not any(
            item.get("status") == "execution_failed"
            for item in natural_action_records
        ),
        "governance-only approval is not recorded as execution failure",
        natural_action_records,
    )

    reviews_before_degraded = get_json("/reviews/actions?limit=200").get("items") or []
    degraded_restart = semantic_understanding(
        natural_restart_text,
        resolver_status="degraded",
        operation="fulfill_open_request",
        target_type="open_goal",
        target_value=natural_restart_text,
    )
    with injected_understanding(degraded_restart):
        degraded_natural_restart = post_json(
            "/events/message",
            {
                "text": natural_restart_text,
                "channel": "api",
                "user_id": "guard-smoke-user",
                "session_id": "guard-smoke-degraded-session",
                "message_id": "guard-natural-restart-degraded",
            },
        )
    degraded_artifacts, _, degraded_policy = decision_artifacts(
        degraded_natural_restart
    )
    expect(
        degraded_natural_restart.get("route") == "ask_user"
        and degraded_natural_restart.get("risk_level") == "R1",
        "degraded natural restart fails closed to clarification",
        degraded_natural_restart,
    )
    expect(
        degraded_policy.get("allowed_effects") == []
        and not degraded_artifacts.get("review"),
        "degraded natural restart grants no effects or review",
        degraded_natural_restart,
    )
    reviews_after_degraded = get_json("/reviews/actions?limit=200").get("items") or []
    expect(
        len(reviews_after_degraded) == len(reviews_before_degraded),
        "degraded natural restart does not mutate the review queue",
        reviews_after_degraded,
    )

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

    db_delete = proposal(
        {
            "proposal_id": "guard_db_delete",
            "agent": "guard-smoke",
            "action": {"type": "shell_command", "command": ["psql", "-c", "DELETE FROM users"]},
            "risk_guess": "R4",
            "reversible": "partial",
            "reason": "Agent requests destructive database delete.",
        }
    )
    expect(db_delete.get("status") in {"blocked", "needs_confirmation"}, "database delete blocked or reviewed", db_delete)
    expect(db_delete.get("tool_trace", {}).get("trace_id"), "database delete trace", db_delete)

    db_drop = proposal(
        {
            "proposal_id": "guard_db_drop",
            "agent": "guard-smoke",
            "action": {"type": "shell_command", "command": ["psql", "-c", "DROP TABLE users"]},
            "risk_guess": "R5",
            "reversible": "no",
            "reason": "Agent attempts destructive database drop.",
        }
    )
    expect(db_drop.get("status") == "blocked", "database drop blocked", db_drop)
    expect(db_drop.get("tool_trace", {}).get("trace_id"), "database drop trace", db_drop)

    tool_logs = get_json("/logs/tools")
    traces = [item for item in tool_logs.get("items", []) if item.get("trace_id")]
    expect(len(traces) >= 12, "all scenarios emitted tool traces", traces)
    audit = get_json("/audit/journal?limit=200")
    proposals = [
        item
        for item in audit.get("items", [])
        if str(item.get("event_id") or "").startswith("guard_") or str(item.get("trace_id") or "").startswith("guard_")
    ]
    expect(len(proposals) >= 12, "all scenarios entered audit", proposals)

    print("Tool Proxy guard smoke passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"not ok - {exc}", file=sys.stderr)
        raise SystemExit(1)
