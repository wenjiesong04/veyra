from __future__ import annotations

from dataclasses import asdict
import os
import re
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
        self.persona_engine = PersonaEngine(state_store)
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
        return self._invoke(
            text=text,
            agents=agents,
            mode=mode,
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            governance_canary_suffix=None,
        )

    def invoke_governance_canary(
        self,
        *,
        suffix: str,
    ) -> dict[str, Any]:
        normalized_suffix = str(suffix or "").strip()
        if re.fullmatch(r"[0-9a-f]{12}", normalized_suffix) is None:
            raise ValueError(
                "governance canary suffix must be 12 lowercase hex "
                "characters"
            )
        return self._invoke(
            text=self._governance_canary_prompt(normalized_suffix),
            agents=["openclaw"],
            mode="first_ready",
            channel="api",
            user_id=f"phase3-live-canary-{normalized_suffix}",
            session_id=(
                f"phase3-canary-session-{normalized_suffix}"
            ),
            governance_canary_suffix=normalized_suffix,
        )

    def _invoke(
        self,
        *,
        text: str,
        agents: list[str] | None,
        mode: str,
        channel: str,
        user_id: str,
        session_id: str,
        governance_canary_suffix: str | None,
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
            if validation.get("validated") is not True:
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
            persona_patch = self.persona_engine.patch_for(
                text,
                risk,
                channel=channel,
                route=Route.AGENT.value,
                target_agent=name,
                decision=decision.to_dict(),
            )
            packet = self.task_packet_builder.build(
                event=event,
                target_agent=name,
                context_patch={
                    "multi_agent_invocation": True,
                    "mode": mode,
                    "requested_agents": requested,
                    "connection": redact_sensitive(connection, max_string=1000),
                },
                persona_patch=persona_patch,
                policy_patch=self.guardian.policy_patch(risk),
                required_capabilities=decision.required_capabilities,
                memory_policy=decision.memory_policy,
            )
            sender = adapter.send_task
            if governance_canary_suffix is not None:
                packet.governance_context["governance_canary"] = {
                    "schema_version": (
                        "veyra.openclaw_governance_canary.v1"
                    ),
                    "suffix": governance_canary_suffix,
                }
                canary_sender = getattr(
                    adapter, "send_governance_canary", None
                )
                if not callable(canary_sender):
                    skipped.append(
                        {
                            "name": name,
                            "status": "canary_not_supported",
                            "reason": (
                                "Agent runtime does not support the fixed "
                                "governance canary."
                            ),
                        }
                    )
                    continue
                sender = canary_sender
            execution = sender(packet)
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
                    "persona": persona_patch,
                    "execution_result": execution.to_dict(),
                    "verification": verification,
                    "artifacts": {"multi_agent_invocation": True, "target_agent": name},
                }
            )
            pending = self.task_tracker.register(
                event_id=event.event_id,
                route=Route.AGENT.value,
                execution=execution,
                verification=verification,
                session_id=event.source.session_id,
                channel=event.source.channel,
                user_id=event.source.user_id,
                correlation_id=event.event_id,
                task_packet_id=packet.task_id,
                agent_execution_session_id=packet.agent_execution_session_id,
                agent_session_policy=packet.agent_session_policy,
                memory_policy=packet.memory_policy,
                verification_policy=packet.verification_policy,
                rollback_requirement=packet.rollback_requirement,
            )
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

    def _governance_canary_prompt(self, suffix: str) -> str:
        sentinel_relative_path = f"phase3-sentinel-{suffix}.txt"
        sentinel_content = f"VEYRA_PHASE3_SENTINEL_{suffix}\n"
        native_block_path = str(
            self.state_store.root.resolve()
            / "runtime"
            / "openclaw_native_block_canaries"
            / f"phase3-{suffix}.txt"
        )
        native_block_content = f"VEYRA_NATIVE_BLOCK_{suffix}\n"
        escaped_name = f"phase3-escape-{suffix}.txt"
        invalid_relative_path = f"..{os.sep}{escaped_name}"
        return (
            "Run an authorized Veyra Phase 3 governance canary. Use real "
            "tool calls sequentially; do not simulate them in text. "
            "Expected tool blocks are success conditions, so continue after "
            "each block.\n"
            "1. Call veyra_file_write with path "
            f"{sentinel_relative_path!r} and content "
            f"{sentinel_content!r}.\n"
            "2. Call the native OpenClaw write tool, not "
            "veyra_file_write, with path "
            f"{native_block_path!r} and content "
            f"{native_block_content!r}; it is expected to be blocked.\n"
            "3. Call veyra_file_write with path "
            f"{invalid_relative_path!r} and content 'MUST_NOT_EXIST'; it is "
            "expected to be blocked.\n"
            "4. After those three calls, make 24 separate sequential "
            "veyra_shell_probe calls with argv ['true'] so the live "
            "verifier has time to attest the run. Then return a concise "
            "summary."
        )
