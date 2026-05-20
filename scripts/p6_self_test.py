from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as app_module  # noqa: E402
from core.agency_core import AgencyCore  # noqa: E402
from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.verifier import Verifier  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from execution.action_executor import ActionExecutor  # noqa: E402
from guardian.review_queue import ReviewQueue  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.event_schema import VeyraTaskPacket  # noqa: E402
from memory_bridge.local_memory_bridge import LocalMemoryBridge  # noqa: E402
from probes.network_probe import NetworkProbe  # noqa: E402
from probes.web_probe import WebProbe  # noqa: E402
from rollback_audit.diff_tracker import DiffTracker  # noqa: E402
from rollback_audit.rollback_manager import RollbackManager  # noqa: E402
from runtime.proactive_checks import ProactiveChecks  # noqa: E402
from runtime.retention_policy import RetentionPolicy  # noqa: E402
from runtime.safety_validation import SafetyValidation  # noqa: E402
from tool_proxy.safe_api import SafeAPI  # noqa: E402
from tool_proxy.safe_browser import SafeBrowser  # noqa: E402
from tool_proxy.safe_file import SafeFile  # noqa: E402
from tool_proxy.safe_shell import SafeShell  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


@dataclass
class FakePollingAdapter(AgentAdapter):
    statuses: list[ExecutionResult]

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        return self.statuses[0]

    def fetch_task_status(self, task_id: str) -> ExecutionResult:
        return self.statuses.pop(0) if self.statuses else ExecutionResult(task_id=task_id, executor="fake", status="success", result="done")

    def fetch_capabilities(self) -> dict[str, Any]:
        return {"runtime": "fake", "status": "available", "connected": True}

    def connection_status(self) -> dict[str, Any]:
        return {"name": "fake", "status": "available", "connected": True}

    def stop_task(self, task_id: str) -> bool:
        return True


def reset_main_state(tmp: Path) -> TestClient:
    state_store = WorldStateStore(tmp / "state")
    agency_root = tmp / "agency"
    agency_root.mkdir(parents=True, exist_ok=True)
    (agency_root / "goals.json").write_text(json.dumps({"selected_agent_must_be_available": True}), encoding="utf-8")
    (agency_root / "triggers.yaml").write_text("triggers:\n  - name: selected_agent_unavailable\n    action: probe_executor_status\n", encoding="utf-8")
    (agency_root / "intention_queue.json").write_text("[]", encoding="utf-8")

    runtime = RuntimeEntity(state_store=state_store)
    loop = AwarenessLoop(state_store=state_store, runtime_entity=runtime)

    app_module.state_store = state_store
    app_module.runtime_entity = runtime
    app_module.awareness_loop = loop
    app_module.review_queue = ReviewQueue(state_store)
    app_module.rollback_manager = RollbackManager(state_store, snapshot_root=str(tmp / "snapshots"))
    app_module.safe_shell = SafeShell(state_store=state_store)
    app_module.safe_file = SafeFile(state_store=state_store)
    app_module.safe_browser = SafeBrowser(state_store=state_store)
    app_module.safe_api = SafeAPI(state_store=state_store)
    app_module.action_executor = ActionExecutor(state_store=state_store)
    app_module.proactive_checks = ProactiveChecks(state_store, agency_root=str(agency_root))
    app_module.diff_tracker = DiffTracker()
    app_module.agency_core = AgencyCore(state_store, agency_root=agency_root)
    app_module.safety_validation = SafetyValidation()
    app_module.retention_policy = RetentionPolicy(state_store)
    return TestClient(app_module.app)


def main() -> int:
    with TemporaryDirectory(prefix="veyra-p6-") as raw_tmp:
        tmp = Path(raw_tmp)
        client = reset_main_state(tmp)

        verifier = Verifier()
        for status in ["submitted", "running", "pending"]:
            verdict = verifier.verify_execution_result(ExecutionResult(task_id=f"t_{status}", executor="fake", status=status, result="queued"))
            expect(verdict["status"] == "partially_success", f"verifier {status}", verdict)
            expect(verdict["next_action"] == "poll_runtime_or_probe_result", f"verifier {status} next action", verdict)

        adapter = FakePollingAdapter(
            [
                ExecutionResult(task_id="fake_1", executor="fake", status="running", result="running"),
                ExecutionResult(task_id="fake_1", executor="fake", status="success", result="done", raw={"evidence": True}),
            ]
        )
        final = adapter.poll_task("fake_1", timeout_seconds=1, interval_seconds=0.01)
        expect(final.status == "success", "agent polling reaches terminal", final)

        r2 = client.post(
            "/actions/proposals",
            json={
                "proposal_id": "p6_r2",
                "agent": "self-test",
                "risk_guess": "R2",
                "action": {"type": "shell_command", "command": ["echo", "p6"]},
            },
        )
        expect(r2.status_code == 200, "R2 proposal accepted", r2.text)
        expect(r2.json()["status"] == "ok", "R2 proposal executed through tool proxy", r2.json())

        r3 = client.post(
            "/actions/proposals",
            json={
                "proposal_id": "p6_r3",
                "agent": "self-test",
                "risk_guess": "R3",
                "action": {"type": "file_write", "path": str(tmp / "config.txt"), "content": "after\n"},
            },
        )
        expect(r3.json()["status"] == "needs_confirmation", "R3 proposal enters review", r3.json())
        review_id = r3.json()["review"]["review_id"]
        approved = client.post(f"/reviews/{review_id}/approve", json={"reason": "p6_self_test"})
        expect(approved.json()["execution_result"]["status"] == "ok", "approved review executes safe file", approved.json())

        r5 = client.post(
            "/actions/proposals",
            json={
                "proposal_id": "p6_r5",
                "agent": "self-test",
                "risk_guess": "R1",
                "action": {"type": "shell_command", "command": ["rm", "-rf", "/tmp/veyra-danger"]},
            },
        )
        expect(r5.json()["status"] == "blocked", "detected R5 overrides low risk guess", r5.json())

        agency = client.post("/proactive/check")
        expect(agency.status_code == 200, "proactive check endpoint", agency.text)
        intentions = client.get("/agency/intentions").json()["intentions"]
        expect(isinstance(intentions, list), "agency intention queue available", intentions)

        network = NetworkProbe().run("localhost")
        web = WebProbe().run("http://127.0.0.1:9/")
        expect(network["probe"] == "network_probe" and "ttl_seconds" in network, "network probe envelope", network)
        expect(web["probe"] == "web_probe" and web["status"] in {"ok", "http_error", "unavailable"}, "web probe envelope", web)

        memory = LocalMemoryBridge(app_module.state_store, adapter_resolver=lambda: adapter)
        safe = memory.write_patch({"session_id": "p6", "task": "remember openclaw probe strategy", "result": "check status"})
        blocked = memory.write_patch({"session_id": "p6", "token": "secret-token"})
        summary = memory.read_summary("p6", ["openclaw"])
        expect(safe["status"] == "written", "memory safe write", safe)
        expect(blocked["status"] == "blocked", "memory sensitive write blocked", blocked)
        expect("summary" in summary and "external_summary" in summary, "memory summary includes external slot", summary)

        polled = client.get("/agent/tasks/task_unknown")
        expect(polled.status_code == 200, "agent task polling endpoint", polled.text)
        stopped = client.post("/agent/tasks/task_unknown/stop")
        expect(stopped.status_code == 200, "agent task stop endpoint", stopped.text)

        red_team = client.get("/ops/safety/red-team").json()
        retention = client.get("/ops/retention").json()
        expect(red_team["status"] == "passed", "P7 red-team safety baseline", red_team)
        expect(retention["status"] == "ok" and retention["files"], "P7 retention policy summary", retention)

    print("P6 self-test passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"not ok - {exc}", file=sys.stderr)
        raise SystemExit(1)
