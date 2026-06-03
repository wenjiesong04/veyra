from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_BOOTSTRAP_RUNTIME = TemporaryDirectory(prefix="veyra-p6-bootstrap-")
_BOOTSTRAP_ROOT = Path(_BOOTSTRAP_RUNTIME.name)
os.environ.setdefault("VEYRA_STATE_ROOT", str(_BOOTSTRAP_ROOT / "state"))
os.environ.setdefault("VEYRA_AGENCY_ROOT", str(_BOOTSTRAP_ROOT / "agency"))

import main as app_module  # noqa: E402
from core.agency_core import AgencyCore  # noqa: E402
from core.capability_registry import CapabilityRegistry  # noqa: E402
from core.context_patch_builder import ContextPatchBuilder  # noqa: E402
from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.decision_core import DecisionCore  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.foresight_engine import ForesightEngine  # noqa: E402
from core.guardian_controller import GuardianController  # noqa: E402
from core.model_client import CoreModelClient  # noqa: E402
from core.perception_layer import PerceptionLayer  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.turn_context_builder import TurnContextBuilder  # noqa: E402
from core.verifier import Verifier  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from execution.action_executor import ActionExecutor  # noqa: E402
from guardian.review_queue import ReviewQueue  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.event_schema import Route, VeyraTaskPacket  # noqa: E402
from memory_bridge.local_memory_bridge import LocalMemoryBridge  # noqa: E402
from probes.network_probe import NetworkProbe  # noqa: E402
from probes.time_probe import TimeProbe  # noqa: E402
from probes.web_probe import WebProbe  # noqa: E402
from rollback_audit.diff_tracker import DiffTracker  # noqa: E402
from rollback_audit.action_journal import ActionJournal  # noqa: E402
from rollback_audit.replay import Replay  # noqa: E402
from rollback_audit.rollback_manager import RollbackManager  # noqa: E402
from runtime.alert_dispatcher import AlertDispatcher  # noqa: E402
from runtime.deployment_config import DeploymentConfigValidator  # noqa: E402
from runtime.external_world_refresh import ExternalWorldRefresh  # noqa: E402
from runtime.ops_monitor import OpsMonitor  # noqa: E402
from runtime.proactive_checks import ProactiveChecks  # noqa: E402
from runtime.retention_policy import RetentionPolicy  # noqa: E402
from runtime.runtime_matrix import RuntimeMatrix  # noqa: E402
from runtime.safety_validation import SafetyValidation  # noqa: E402
from runtime.soak_runner import SoakRunner  # noqa: E402
from runtime.state_refresh import StateRefresh  # noqa: E402
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

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "summary": f"fake memory for {session_id}", "freshness": "fresh", "trust": "external"}

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> None:
        return None


class FakeCoreReasoning:
    def __init__(
        self,
        decision: dict[str, Any] | None = None,
        perception: dict[str, Any] | None = None,
        agency: dict[str, Any] | None = None,
        foresight: dict[str, Any] | None = None,
        memory: dict[str, Any] | None = None,
        external_world: dict[str, Any] | None = None,
        answer: dict[str, Any] | None = None,
        probe_answer: dict[str, Any] | None = None,
    ) -> None:
        self.decision = decision or {"status": "skipped"}
        self.perception = perception or {"status": "skipped"}
        self.agency = agency or {"status": "skipped"}
        self.foresight = foresight or {"status": "skipped"}
        self.memory = memory or {"status": "skipped"}
        self.external_world = external_world or {"status": "skipped"}
        self.answer = answer or {"status": "skipped"}
        self.probe_answer = probe_answer or {"status": "skipped"}

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "configured": True, "decision_mode": "always", "status": "configured"}

    def is_enabled(self) -> bool:
        return True

    def should_assist(self, kind: str, rule_context: dict[str, Any]) -> bool:
        return True

    def decision_assist(self, *, text: str, attention_focus: list[str], rule_decision: dict[str, Any], event: Any = None) -> dict[str, Any]:
        return self.decision

    def answer_assist(self, *, text: str, attention_focus: list[str], decision: dict[str, Any], event: Any = None) -> dict[str, Any]:
        return self.answer

    def probe_answer_assist(
        self,
        *,
        text: str,
        attention_focus: list[str],
        probe_result: dict[str, Any],
        decision: dict[str, Any],
        event: Any = None,
    ) -> dict[str, Any]:
        return self.probe_answer

    def perception_assist(self, probe_result: dict[str, Any]) -> dict[str, Any]:
        return self.perception

    def agency_assist(self, *, goals: dict[str, Any], world_state: dict[str, Any], rule_gaps: list[dict[str, Any]]) -> dict[str, Any]:
        return self.agency

    def foresight_assist(self, *, text: str, risk_level: str, rule_foresight: dict[str, Any], decision: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.foresight

    def memory_assist(self, *, session_id: str, focus: list[str], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        return self.memory

    def external_world_assist(self, *, target: str, probe_result: dict[str, Any], current_goal: str = "") -> dict[str, Any]:
        return self.external_world


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
    app_module.action_journal = ActionJournal(state_store)
    app_module.replay_engine = Replay(state_store)
    app_module.safe_shell = SafeShell(state_store=state_store)
    app_module.safe_file = SafeFile(state_store=state_store)
    app_module.safe_browser = SafeBrowser(state_store=state_store)
    app_module.safe_api = SafeAPI(state_store=state_store)
    app_module.action_executor = ActionExecutor(
        state_store=state_store,
        safe_shell=app_module.safe_shell,
        safe_file=app_module.safe_file,
        safe_browser=app_module.safe_browser,
        safe_api=app_module.safe_api,
        rollback_manager=app_module.rollback_manager,
    )
    app_module.foresight_engine = ForesightEngine(reasoning=loop.core_reasoning)
    app_module.proactive_checks = ProactiveChecks(state_store, agency_root=str(agency_root), reasoning=loop.core_reasoning)
    app_module.diff_tracker = DiffTracker()
    app_module.agency_core = AgencyCore(state_store, agency_root=agency_root, reasoning=loop.core_reasoning)
    app_module.safety_validation = SafetyValidation()
    app_module.retention_policy = RetentionPolicy(state_store)
    app_module.state_refresh = StateRefresh(state_store, reasoning=loop.core_reasoning)
    app_module.external_world_refresh = ExternalWorldRefresh(state_store, reasoning=loop.core_reasoning)
    app_module.ops_monitor = OpsMonitor(
        state_store,
        agent_status_resolver=lambda: loop.agent_registry.selected().connection_status(),
        retention_policy=app_module.retention_policy,
        safety_validation=app_module.safety_validation,
    )
    app_module.alert_dispatcher = AlertDispatcher(state_store, app_module.ops_monitor)
    app_module.deployment_validator = DeploymentConfigValidator(state_store)
    app_module.runtime_matrix = RuntimeMatrix(state_store, loop.agent_registry, loop.memory_bridge)
    app_module.soak_runner = SoakRunner(
        proactive_checks=app_module.proactive_checks,
        task_tracker=loop.task_tracker,
        state_refresh=app_module.state_refresh,
        retention_policy=app_module.retention_policy,
        safety_validation=app_module.safety_validation,
        adapter_resolver=lambda: loop.agent_registry.selected(),
        verifier=loop.verifier,
    )
    return TestClient(app_module.app)


def main() -> int:
    with TemporaryDirectory(prefix="veyra-p6-") as raw_tmp:
        tmp = Path(raw_tmp)
        client = reset_main_state(tmp)

        verifier = Verifier()
        policy_patch = GuardianController().policy_patch(RiskLevel.R2)
        expect(policy_patch["tool_proxy_contract"]["proposal_endpoint"] == "/actions/proposals", "agent policy patch includes tool proxy contract", policy_patch)
        for status in ["submitted", "running", "pending"]:
            verdict = verifier.verify_execution_result(ExecutionResult(task_id=f"t_{status}", executor="fake", status=status, result="queued"))
            expect(verdict["status"] == "partially_success", f"verifier {status}", verdict)
            expect(verdict["next_action"] == "poll_runtime_or_probe_result", f"verifier {status} next action", verdict)
        failed_placeholder = verifier.verify_execution_result(
            ExecutionResult(task_id="agent_failed_placeholder", executor="openclaw", status="success", result="[assistant turn failed before producing content]")
        )
        expect(failed_placeholder["status"] == "verified_failed", "verifier rejects agent failure placeholder", failed_placeholder)
        expect(failed_placeholder["verdict"] == "execution_result_contains_failure_marker", "verifier failure placeholder verdict", failed_placeholder)

        bypass = verifier.verify_execution_result(
            ExecutionResult(task_id="tool_bypass", executor="fake", status="success", result="restarted", tool_calls=["sudo restart openclaw"])
        )
        expect(bypass["verdict"] == "tool_proxy_bypass_suspected", "verifier flags unproxied high-risk agent tool call", bypass)
        proxied = verifier.verify_execution_result(
            ExecutionResult(
                task_id="tool_proxied",
                executor="fake",
                status="success",
                result="restarted",
                tool_calls=["sudo restart openclaw"],
                raw={"action_proposals": [{"status": "approved", "review_id": "rev_1"}]},
            )
        )
        expect(proxied["status"] == "verified_success", "verifier accepts approved high-risk agent tool call", proxied)
        forbidden = verifier.verify_execution_result(
            ExecutionResult(task_id="tool_forbidden", executor="fake", status="success", result="deleted", tool_calls=["rm -rf /tmp/veyra-danger"])
        )
        expect(forbidden["verdict"] == "forbidden_tool_call_reported", "verifier blocks forbidden agent tool call", forbidden)

        adapter = FakePollingAdapter(
            [
                ExecutionResult(task_id="fake_1", executor="fake", status="running", result="running"),
                ExecutionResult(task_id="fake_1", executor="fake", status="success", result="done", raw={"evidence": True}),
            ]
        )
        final = adapter.poll_task("fake_1", timeout_seconds=1, interval_seconds=0.01)
        expect(final.status == "success", "agent polling reaches terminal", final)
        tracker = app_module.awareness_loop.task_tracker
        pending_execution = ExecutionResult(task_id="retry_later", executor="fake", status="running", result="queued")
        tracker.register(
            event_id="evt_retry_later",
            route="agent",
            execution=pending_execution,
            verification={"status": "partially_success", "next_action": "poll_runtime_or_probe_result"},
        )
        tracker.apply_result(
            execution=ExecutionResult(task_id="retry_later", executor="fake", status="adapter_unconfigured", result="temporarily unavailable"),
            verification={"status": "needs_more_probe", "next_action": "configure_or_refresh_agent_runtime"},
        )
        still_pending = app_module.state_store.read_json("task_state.json").get("pending_agent_tasks", [])
        expect(any(item.get("task_id") == "retry_later" for item in still_pending), "transient adapter poll keeps pending task", still_pending)

        model_decision = DecisionCore(
            app_module.state_store,
            reasoning=FakeCoreReasoning(
                decision={
                    "status": "model_assisted",
                    "route": "agent",
                    "risk_level": "R1",
                    "intent": "implementation",
                    "complexity": "complex",
                    "reason": "needs selected agent runtime with context patch",
                    "signals": ["model:implementation"],
                    "solution_outline": ["read state", "delegate bounded implementation", "verify result"],
                    "agent_context": {"handoff": "include state and proposed solution"},
                }
            ),
        ).decide("完善 Veyra 的模型认知链路", [])
        expect(model_decision.route == Route.AGENT, "core model can select agent route", model_decision.to_dict())
        expect(bool(model_decision.model_assist.get("solution_outline")), "core model keeps solution outline", model_decision.to_dict())

        blocked_decision = DecisionCore(
            app_module.state_store,
            reasoning=FakeCoreReasoning(decision={"status": "model_assisted", "route": "direct_answer", "risk_level": "R0", "reason": "safe"}),
        ).decide("rm -rf /tmp/veyra-danger", [])
        expect(blocked_decision.route == Route.BLOCK and blocked_decision.risk_level.value == "R5", "core model cannot lower R5 risk", blocked_decision.to_dict())

        time_decision = DecisionCore(app_module.state_store, reasoning=FakeCoreReasoning()).decide("现在东京时间是几点", [])
        expect(time_decision.route == Route.PROBE and time_decision.selected_probe == "time", "volatile time question routes to time probe", time_decision.to_dict())
        time_probe = TimeProbe().run("现在东京时间是几点")
        expect(time_probe["probe"] == "time_probe" and time_probe["details"]["timezone"] == "Asia/Tokyo", "time probe returns Tokyo evidence", time_probe)
        time_preserved = DecisionCore(
            app_module.state_store,
            reasoning=FakeCoreReasoning(decision={"status": "model_assisted", "route": "direct_answer", "risk_level": "R0", "reason": "guessed", "draft_response": "猜一个时间"}),
        ).decide("现在东京时间是几点", [])
        expect(
            time_preserved.route == Route.PROBE
            and (
                "policy:required_probe_preserved" in time_preserved.signals
                or "policy:rule_probe_route_preserved" in time_preserved.signals
            ),
            "model cannot replace required fresh probe with guess",
            time_preserved.to_dict(),
        )

        capabilities = CapabilityRegistry(app_module.state_store).snapshot()
        expect(capabilities["capabilities"]["time_probe"]["available"] is True, "capability registry exposes time probe", capabilities)
        expect(capabilities["capabilities"]["weather_probe"]["available"] is True, "weather_probe is a native read-only probe", capabilities)
        turn_context = TurnContextBuilder(app_module.state_store).build(user_message="今天北京天气怎么样", attention_focus=["weather"])
        expect("available_capabilities" in turn_context and "local_world" not in turn_context, "turn context is scoped and capability-aware", turn_context)
        weather_decision = DecisionCore(app_module.state_store, reasoning=FakeCoreReasoning()).decide("今天北京天气怎么样", [])
        expect(
            weather_decision.route == Route.PROBE
            and weather_decision.freshness_required
            and weather_decision.selected_probe == "weather_probe"
            and "weather_probe" in weather_decision.required_capabilities,
            "weather freshness routes to weather_probe",
            weather_decision.to_dict(),
        )
        weather_loop = client.post(
            "/events/message",
            json={"text": "今天北京天气怎么样", "channel": "self-test", "user_id": "p6", "session_id": "p6-weather"},
        ).json()
        expect(weather_loop["route"] == "probe", "controller runs weather probe when capability is available", weather_loop)

        agent_rejected = DecisionCore(
            app_module.state_store,
            reasoning=FakeCoreReasoning(
                decision={
                    "status": "model_assisted",
                    "route": "agent",
                    "risk_level": "R0",
                    "intent": "conversation",
                    "complexity": "simple",
                    "reason": "mistaken delegation",
                    "draft_response": "这是普通对话，不需要 Agent。",
                }
            ),
        ).decide("你现在是谁在接收消息", [])
        expect(agent_rejected.route == Route.DIRECT_ANSWER and "policy:agent_route_rejected" in agent_rejected.signals, "simple chat cannot be escalated to agent by model alone", agent_rejected.to_dict())

        perception = PerceptionLayer(
            app_module.state_store,
            reasoning=FakeCoreReasoning(
                perception={
                    "status": "model_assisted",
                    "summary": "OpenClaw appears unavailable because the port refused connections.",
                    "claims": [{"key": "model:openclaw:unavailable", "claim": "OpenClaw needs runtime availability review", "confidence": 0.7}],
                }
            ),
        )
        perception_patch = perception.interpret_probe_result(
            {
                "probe": "openclaw_probe",
                "status": "closed",
                "summary": "OpenClaw runtime port 18789 is closed.",
                "confidence": 0.8,
                "ttl_seconds": 30,
                "details": {"error": "connection refused"},
            }
        )
        expect(any(str(claim.get("source", "")).startswith("core_model:") for claim in perception_patch["belief.claims"]), "core model perception creates grounded claim", perception_patch)

        model_agency = AgencyCore(
            app_module.state_store,
            agency_root=tmp / "agency",
            reasoning=FakeCoreReasoning(
                agency={
                    "status": "model_assisted",
                    "state_gaps": [
                        {
                            "gap_id": "missing_runtime_model",
                            "target": "core_model",
                            "observed_status": "unconfigured",
                            "risk_level": "R2",
                            "suggested_action": "suggest_core_model_setup",
                            "action_text": "suggest configuring a Core model for richer planning",
                        }
                    ],
                }
            ),
        )
        model_gaps = model_agency.detect_state_gap(goals={}, world_state=app_module.state_store.read_all())
        expect(any(gap.get("gap_id") == "model:missing_runtime_model" for gap in model_gaps), "core model can add agency state gap", model_gaps)

        model_foresight = ForesightEngine(
            reasoning=FakeCoreReasoning(
                foresight={
                    "status": "model_assisted",
                    "reversible": "full",
                    "impact_summary": "Restart can interrupt active sessions.",
                    "side_effects": ["active session interruption"],
                    "required_preconditions": ["confirm rollback command"],
                    "safer_alternatives": ["probe logs before restart"],
                    "unsafe_assumptions": ["service is stateless"],
                    "confidence": 0.72,
                }
            )
        ).predict_text_action("restart openclaw service", RiskLevel.R4, decision={"route": "human_review", "risk_level": "R4", "complexity": "moderate"})
        expect(model_foresight["reversible"] == "partial", "model foresight cannot make rule impact less cautious", model_foresight)
        expect("confirm rollback command" in model_foresight["required_preconditions"], "model foresight adds preconditions", model_foresight)

        context_patch = ContextPatchBuilder(app_module.state_store).build(
            "restart openclaw service",
            ["openclaw"],
            decision={"route": "human_review", "risk_level": "R4"},
            foresight=model_foresight,
        )
        expect("decision_trace" in context_patch and "foresight" in context_patch and "executor_state" in context_patch, "context patch carries governance background", context_patch)

        app_module.state_store.write_json(
            "agent_memory.json",
            {
                "items": [
                    {"patch": {"task": "unrelated", "result": "ignore"}, "freshness": "fresh", "trust": "observed"},
                    {"patch": {"task": "openclaw restart", "result": "check gateway token first"}, "freshness": "fresh", "trust": "observed"},
                    {"patch": {"task": "frontend css", "result": "not relevant"}, "freshness": "fresh", "trust": "observed"},
                ]
            },
        )
        model_memory = LocalMemoryBridge(
            app_module.state_store,
            adapter_resolver=lambda: adapter,
            adapter_getter=lambda provider: adapter,
            provider_names=lambda: ["openclaw", "hermes", "custom"],
            reasoning=FakeCoreReasoning(memory={"status": "model_assisted", "selected_indexes": [1], "relevance_notes": "OpenClaw memory is most relevant."}),
        ).read_summary("p6", ["openclaw"])
        expect(len(model_memory["summary"]) == 1 and "openclaw" in str(model_memory["summary"][0]).lower(), "core model ranks memory relevance", model_memory)
        routed_memory = LocalMemoryBridge(
            app_module.state_store,
            adapter_resolver=lambda: adapter,
            adapter_getter=lambda provider: adapter,
            provider_names=lambda: ["openclaw", "hermes", "custom"],
        )
        provider_status = routed_memory.provider_status()
        all_summary = routed_memory.read_summary("p6", provider="all")
        hermes_write = routed_memory.write_patch({"session_id": "p6", "task": "route memory", "result": "ok"}, provider="hermes")
        hermes_diagnostics = routed_memory.provider_diagnostics(provider="hermes", session_id="p6", write_probe=True)
        all_diagnostics = routed_memory.provider_diagnostics(provider="all", session_id="p6")
        expect("hermes" in provider_status["providers"] and "all" in provider_status["providers"], "memory providers are explicit", provider_status)
        expect(all_summary["external_summary"]["provider"] == "all" and all_summary["external_summary"]["summary"], "memory summary can fan out across providers", all_summary)
        expect(hermes_write["external_write"]["provider"] == "hermes" and hermes_write["external_write"]["status"] == "submitted", "memory patch can target provider", hermes_write)
        expect(hermes_diagnostics["status"] == "success" and hermes_diagnostics["write_probe"]["status"] == "submitted", "memory provider diagnostics write probe", hermes_diagnostics)
        expect(all_diagnostics["provider"] == "all" and all_diagnostics["results"], "memory provider diagnostics fan out", all_diagnostics)

        app_module.state_store.write_json("external_world.json", {"watchlist": [{"target": "localhost", "reason": "self-test"}], "summaries": []})
        external = ExternalWorldRefresh(
            app_module.state_store,
            reasoning=FakeCoreReasoning(
                external_world={
                    "status": "model_assisted",
                    "summary": "localhost remains relevant for local runtime diagnostics",
                    "relevance": "runtime_probe",
                    "watch_recommendation": "keep",
                    "reasons": ["local runtime target"],
                }
            ),
        ).refresh_watchlist(limit=1)
        expect(external["refreshed"] and external["refreshed"][0]["model_assist"]["status"] == "model_assisted", "external world watchlist uses core model interpretation", external)

        agent_model_store = WorldStateStore(tmp / "agent-model-state")
        agent_model_config = agent_model_store.read_json("agent_config.json")
        agent_model_config["selected_agent"] = "openclaw"
        agent_model_config["agents"]["openclaw"].update(
            {
                "use_model_for_core": True,
                "model_base_url": "http://agent-model.local/v1",
                "model_api_key_env": "AGENT_MODEL_KEY",
                "model": "agent-runtime-model",
            }
        )
        agent_model_store.write_json("agent_config.json", agent_model_config)
        agent_model_status = CoreModelClient(agent_model_store).status()
        expect(
            agent_model_status["configured"] and agent_model_status["api_key_env"] == "AGENT_MODEL_KEY",
            "selected agent model config can power Veyra Core",
            agent_model_status,
        )

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

        tool_proxy_status = client.get("/tool-proxy/status").json()
        tool_proxy_config = client.post(
            "/tool-proxy/config",
            json={"api_executor_enabled": True, "browser_executor_enabled": False, "api_allowed_hosts": ["127.0.0.1"]},
        ).json()
        expect(tool_proxy_status["api"]["configured"] is False and tool_proxy_status["browser"]["configured"] is False, "tool proxy executor status defaults closed", tool_proxy_status)
        expect(tool_proxy_config["api"]["configured"] is True and tool_proxy_config["api"]["allowed_hosts"] == ["127.0.0.1"], "tool proxy executor config endpoint", tool_proxy_config)
        client.post("/tool-proxy/config", json={"api_executor_enabled": False, "browser_executor_enabled": False})

        class APIHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def log_message(self, format: str, *args: Any) -> None:
                return None

        server = HTTPServer(("127.0.0.1", 0), APIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api_executor = SafeAPI(state_store=app_module.state_store, executor_configured=True)
            api_result = api_executor.request({"method": "GET", "url": f"http://127.0.0.1:{server.server_port}/status"})
            api_blocked = SafeAPI(state_store=app_module.state_store, executor_configured=True, allowed_hosts=["example.com"]).request(
                {"method": "GET", "url": f"http://127.0.0.1:{server.server_port}/status"}
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
        browser_executor = SafeBrowser(state_store=app_module.state_store, executor=lambda url: {"status": "ok", "opened": True, "url": url})
        browser_blocked = SafeBrowser(
            state_store=app_module.state_store,
            executor=lambda url: {"status": "ok", "opened": True, "url": url},
            allowed_hosts=["example.com"],
        )
        browser_result = browser_executor.open("http://localhost:18789/")
        browser_blocked_result = browser_blocked.open("http://localhost:18789/")
        expect(api_result["status"] == "ok" and api_result["status_code"] == 200, "safe api real executor hook", api_result)
        expect(api_blocked["status"] == "blocked" and "allowlist" in api_blocked["reason"], "safe api executor allowlist blocks", api_blocked)
        expect(browser_result["status"] == "ok" and browser_result["opened"] is True, "safe browser executor hook", browser_result)
        expect(browser_blocked_result["status"] == "blocked" and "allowlist" in browser_blocked_result["reason"], "safe browser executor allowlist blocks", browser_blocked_result)

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

        providers_response = client.get("/memory/providers")
        provider_diagnostics_response = client.get("/memory/providers/diagnostics?provider=local&session_id=p6")
        summary_response = client.get("/memory/summary?session_id=p6&provider=local")
        patch_response = client.post("/memory/patch", json={"provider": "local", "patch": {"session_id": "p6", "task": "local provider", "result": "ok"}})
        expect(providers_response.status_code == 200 and "selected" in providers_response.json()["providers"], "memory providers endpoint", providers_response.text)
        expect(provider_diagnostics_response.status_code == 200 and provider_diagnostics_response.json()["status"] == "success", "memory provider diagnostics endpoint", provider_diagnostics_response.text)
        expect(summary_response.status_code == 200 and summary_response.json()["provider"] == "local", "memory summary provider endpoint", summary_response.text)
        expect(patch_response.status_code == 200 and patch_response.json()["external_write"]["status"] == "local_only", "memory patch provider endpoint", patch_response.text)

        polled = client.get("/agent/tasks/task_unknown")
        expect(polled.status_code == 200, "agent task polling endpoint", polled.text)
        stopped = client.post("/agent/tasks/task_unknown/stop")
        expect(stopped.status_code == 200, "agent task stop endpoint", stopped.text)

        callback = client.post(
            "/agent/results",
            json={"task_id": "callback_1", "executor": "fake", "status": "success", "result": "callback done", "raw": {"session_id": "p6"}},
        )
        expect(callback.status_code == 200 and callback.json()["status"] == "verified_success", "agent result callback", callback.text)

        trace_id = callback.json()["execution_trace"]["trace_id"]
        journal = client.get(f"/audit/journal?trace_id={trace_id}")
        replay = client.get(f"/audit/replay/{trace_id}")
        travel = client.get("/audit/time-travel?limit=20")
        expect(journal.status_code == 200 and journal.json()["items"], "action journal trace lookup", journal.text)
        expect(replay.status_code == 200 and replay.json()["status"] == "planned", "audit replay plan endpoint", replay.text)
        expect(travel.status_code == 200 and travel.json()["last_known"], "audit time-travel endpoint", travel.text)

        restore_target = tmp / "restore_me.txt"
        restore_target.write_text("before\n", encoding="utf-8")
        write_for_restore = client.post(
            "/tool-proxy/file/write",
            json={"path": str(restore_target), "content": "after\n", "reason": "p6 replay compensation"},
        ).json()
        restore_trace_id = write_for_restore["tool_trace"]["trace_id"]
        replay_proposal = client.post(f"/audit/replay/{restore_trace_id}/propose").json()
        restore_review_id = replay_proposal["review"]["review_id"]
        approved_restore = client.post(f"/reviews/{restore_review_id}/approve", json={"reason": "p6 replay restore"}).json()
        expect(replay_proposal["status"] == "needs_confirmation", "audit replay compensation review endpoint", replay_proposal)
        expect(approved_restore["execution_result"]["status"] == "restored" and restore_target.read_text(encoding="utf-8") == "before\n", "approved replay compensation restores snapshot", approved_restore)

        app_module.state_store.write_json(
            "belief_state.json",
            {
                "claims": [
                    {
                        "key": "local_system:platform",
                        "claim": "stale platform",
                        "source": "system_probe",
                        "status": "stale",
                        "next_action": "refresh_probe",
                        "confidence": 0.5,
                        "evidence": {},
                    }
                ]
            },
        )
        stale_refresh = client.post("/state/refresh-stale")
        expect(stale_refresh.status_code == 200 and stale_refresh.json()["refreshed"], "stale belief refresh endpoint", stale_refresh.text)

        watch = client.post("/external/watchlist", json={"target": "localhost", "reason": "p6_self_test"})
        expect(watch.status_code == 200 and watch.json()["watchlist"], "external watchlist endpoint", watch.text)
        external_refresh = client.post("/external/refresh?limit=1")
        expect(external_refresh.status_code == 200 and external_refresh.json()["refreshed"], "external refresh endpoint", external_refresh.text)

        red_team = client.get("/ops/safety/red-team").json()
        retention = client.get("/ops/retention").json()
        for idx in range(5):
            app_module.state_store.append_jsonl("event_log.jsonl", {"idx": idx, "source": "retention_self_test"})
        retention_preview = client.post("/ops/retention/enforce", json={"dry_run": True, "limit_overrides": {"event_log.jsonl": 2}}).json()
        retention_enforce = client.post("/ops/retention/enforce", json={"limit_overrides": {"event_log.jsonl": 2}}).json()
        retained_events = app_module.state_store.read_jsonl("event_log.jsonl", limit=10)
        archive_path = next(item for item in retention_enforce["files"] if item["file"] == "event_log.jsonl")["archive_path"]
        health = client.get("/ops/health").json()
        alerts = client.get("/ops/alerts").json()
        deployment = client.get("/ops/deployment").json()
        deployment_config = client.get("/ops/deployment/config").json()
        runtime_matrix_before = client.get("/ops/runtime-matrix").json()
        runtime_matrix = client.post("/ops/runtime-matrix/run").json()
        alerting = client.get("/ops/alerting").json()
        alert_config = client.post("/ops/alerting/config", json={"webhook_enabled": False, "min_severity": "info"}).json()
        alert_dispatch = client.post("/ops/alerts/dispatch?min_severity=info").json()
        alert_log = client.get("/logs/alerts").json()
        expect(red_team["status"] == "passed", "P7 red-team safety baseline", red_team)
        expect(retention["status"] == "ok" and retention["files"], "P7 retention policy summary", retention)
        expect(retention_preview["dry_run"] is True and retention_preview["changed"] >= 1, "P7 retention enforce dry run", retention_preview)
        expect(retention_enforce["changed"] >= 1 and retained_events[-1]["idx"] == 4 and len(retained_events) == 2, "P7 retention enforce truncates after archive", retention_enforce)
        expect((app_module.state_store.root / archive_path).exists(), "P7 retention archive written", archive_path)
        expect(health["status"] in {"healthy", "degraded", "critical"} and "components" in health, "P7 ops health endpoint", health)
        expect(alerts["status"] == "success" and "summary" in alerts, "P7 ops alerts endpoint", alerts)
        expect(
            deployment["status"] in {"ready", "degraded", "not_ready", "not_configured", "validation_pending"}
            and deployment["checks"]
            and "configuration" in deployment
            and "validation" in deployment,
            "P7 deployment readiness endpoint",
            deployment,
        )
        expect(deployment_config["status"] in {"ready", "ready_with_warnings", "not_ready"} and deployment_config["checks"], "P7 deployment config validation endpoint", deployment_config)
        expect(runtime_matrix_before["status"] in {"not_run", "ready", "degraded", "not_configured"}, "P7 runtime matrix status endpoint", runtime_matrix_before)
        expect(runtime_matrix["status"] in {"ready", "degraded", "not_configured"} and runtime_matrix["runtimes"], "P7 runtime matrix run endpoint", runtime_matrix)
        expect(alerting["local_log"] is True and "recent" in alerting, "P7 alerting status endpoint", alerting)
        expect(alert_config["min_severity"] == "info", "P7 alerting config endpoint", alert_config)
        expect(alert_dispatch["status"] == "success" and alert_dispatch["external"]["status"] in {"disabled", "skipped"}, "P7 alert dispatch endpoint", alert_dispatch)
        expect(alert_log["items"], "P7 alert log endpoint", alert_log)

        soak = client.post("/ops/soak", json={"iterations": 1})
        expect(soak.status_code == 200 and soak.json()["status"] == "success", "ops soak endpoint", soak.text)
        soak_status = client.get("/ops/soak/status").json()
        soak_start = client.post("/ops/soak/start", json={"iterations": 2, "interval_seconds": 0}).json()
        for _ in range(20):
            soak_session = client.get("/ops/soak/status").json()
            if soak_session.get("status") in {"completed", "failed"}:
                break
            time.sleep(0.02)
        soak_start_stop = client.post("/ops/soak/start", json={"iterations": 10, "interval_seconds": 1}).json()
        soak_stop = client.post("/ops/soak/stop").json()
        expect(soak_status["status"] in {"idle", "completed", "stale"}, "ops soak status endpoint", soak_status)
        expect(soak_start.get("run_id") and soak_session["status"] in {"completed", "failed"}, "ops soak session start/status endpoint", soak_session)
        expect(soak_start_stop.get("run_id") and soak_stop["status"] in {"stopping", "stopped", "completed", "stale"}, "ops soak session stop endpoint", soak_stop)

        model_status = client.get("/core/model/status")
        expect(model_status.status_code == 200 and model_status.json()["status"] == "unconfigured", "core model status endpoint", model_status.text)
        rejected_key = client.post(
            "/core/model/config",
            json={
                "enabled": True,
                "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:65535/v1",
                "model": "veyra-self-test-model",
                "api_key": "secret-self-test-key",
            },
        )
        expect(rejected_key.status_code == 422, "core model rejects direct api key", rejected_key.text)
        configured_model = client.post(
            "/core/model/config",
            json={
                "enabled": True,
                "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:65535/v1",
                "model": "veyra-self-test-model",
                "api_key_env": "VEYRA_CORE_MODEL_API_KEY",
                "decision_mode": "always",
            },
        )
        expect(configured_model.status_code == 200 and configured_model.json()["configured"], "core model config endpoint", configured_model.text)
        expect(configured_model.json()["api_key_env"] == "VEYRA_CORE_MODEL_API_KEY", "core model status keeps api key env", configured_model.json())
        state_payload = client.get("/state").json()
        expect(state_payload["agent_config"]["core_model"].get("api_key") in {"", None}, "state endpoint has no direct core model api key", state_payload["agent_config"]["core_model"])

    print("P6 self-test passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"not ok - {exc}", file=sys.stderr)
        raise SystemExit(1)
