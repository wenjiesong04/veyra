from __future__ import annotations

from dataclasses import asdict
from typing import Any

from core.definitions import RiskLevel, classify_text_risk
from core.guardian_controller import GuardianController
from core.model_client import redact_sensitive
from core.persona_engine import PersonaEngine
from core.task_packet_builder import TaskPacketBuilder
from core.world_state import WorldStateStore
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.agent_registry import AgentRegistry
from interface.event_normalizer import EventNormalizer
from interface.event_schema import Decision, Route
from runtime.agent_task_tracker import AgentTaskTracker
from rollback_audit.execution_trace import ExecutionTrace


class AgentOrchestrator:
    """Runs one user request across one or more configured Agent runtimes."""

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        registry: AgentRegistry,
        task_tracker: AgentTaskTracker,
        execution_trace: ExecutionTrace,
        verifier: Any,
    ) -> None:
        self.state_store = state_store
        self.registry = registry
        self.task_tracker = task_tracker
        self.execution_trace = execution_trace
        self.verifier = verifier
        self.normalizer = EventNormalizer()
        self.task_packet_builder = TaskPacketBuilder(state_store)
        self.persona_engine = PersonaEngine()
        self.guardian = GuardianController()

    def invoke(
        self,
        *,
        text: str,
        agents: list[str] | None = None,
        mode: str = "fanout",
        channel: str = "api",
        user_id: str = "local-user",
        session_id: str = "multi-agent",
    ) -> dict[str, Any]:
        mode = mode if mode in {"fanout", "first_ready"} else "fanout"
        risk = classify_text_risk(text)
        if risk == RiskLevel.R5:
            return {"status": "blocked", "risk_level": risk.value, "reason": "R5 requests are blocked before Agent dispatch.", "results": [], "skipped": []}
        if risk in {RiskLevel.R3, RiskLevel.R4}:
            return {
                "status": "needs_confirmation",
                "risk_level": risk.value,
                "reason": "R3/R4 Agent dispatch requires a reviewed ActionProposal or normal Veyra review flow.",
                "results": [],
                "skipped": [],
            }

        event = self.normalizer.user_message(text=text, channel=channel, user_id=user_id, session_id=session_id)
        requested = [str(name).strip() for name in (agents or []) if str(name).strip()]
        if not requested:
            requested = [self.registry.selected_name()]
        seen: set[str] = set()
        requested = [name for name in requested if not (name in seen or seen.add(name))]

        results: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        available = set(self.registry.names())
        for name in requested:
            if name not in available:
                skipped.append({"name": name, "status": "unknown_agent", "reason": "No adapter is registered for this name."})
                continue
            adapter = self.registry.get(name)
            connection = adapter.connection_status()
            validation = connection.get("validation") if isinstance(connection.get("validation"), dict) else {}
            if not bool(validation.get("validated") or connection.get("connected")):
                skipped.append(
                    {
                        "name": name,
                        "status": str(connection.get("status") or "not_configured"),
                        "reason": "Agent runtime is not validated; skipping real dispatch.",
                        "validation": redact_sensitive(validation),
                    }
                )
                continue

            decision = Decision(
                route=Route.AGENT,
                risk_level=risk,
                reason="explicit multi-agent invocation",
                intent="action",
                complexity="simple",
                capability="agent",
                signals=[f"risk:{risk.value}", f"agent:{name}", "explicit_agent_invocation"],
                constraints=["dispatch only to validated Agent adapters", "return execution evidence"],
                target_agent=name,
            )
            guardian = self.guardian.review_text_action(text, decision, {"risk_level": risk.value, "reversible": "full", "side_effects": [], "required_preconditions": []})
            packet = self.task_packet_builder.build(
                event=event,
                target_agent=name,
                context_patch={
                    "multi_agent_invocation": True,
                    "mode": mode,
                    "requested_agents": requested,
                    "connection": redact_sensitive(connection, max_string=1000),
                },
                persona_patch=self.persona_engine.patch_for(text, risk),
                policy_patch=self.guardian.policy_patch(risk),
            )
            execution = adapter.send_task(packet)
            if execution.status in NON_TERMINAL_STATUSES:
                execution = adapter.poll_task(execution.task_id, timeout_seconds=0)
            verification = self.verifier.verify_execution_result(execution)
            trace = self.execution_trace.record(
                {
                    "event_id": event.event_id,
                    "route": Route.AGENT.value,
                    "task_id": execution.task_id,
                    "executor": execution.executor,
                    "status": verification["status"],
                    "decision": decision.to_dict(),
                    "guardian": guardian,
                    "execution_result": execution.to_dict(),
                    "verification": verification,
                    "artifacts": {"multi_agent_invocation": True, "target_agent": name},
                }
            )
            pending = self.task_tracker.register(event_id=event.event_id, route=Route.AGENT.value, execution=execution, verification=verification)
            results.append(
                {
                    "name": name,
                    "packet": packet.to_dict(),
                    "execution_result": asdict(execution),
                    "verification": verification,
                    "execution_trace": trace,
                    "pending_task": pending,
                }
            )
            if mode == "first_ready":
                break

        status = "success" if results and not skipped else "partial_success" if results else "no_validated_agents"
        output = {
            "status": status,
            "mode": mode,
            "risk_level": risk.value,
            "requested_agents": requested,
            "result_count": len(results),
            "skipped_count": len(skipped),
            "results": results,
            "skipped": skipped,
        }
        self.state_store.append_jsonl("action_record.jsonl", {"route": "multi_agent_invoke", "status": status, "artifacts": redact_sensitive(output, max_string=2200)})
        return output
