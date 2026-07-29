#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from core.task_packet_builder import TaskPacketBuilder  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.agent_dialogue_contract import (  # noqa: E402
    build_collaboration_binding,
    build_task_request,
    scope_digest,
)
from interface.event_schema import (  # noqa: E402
    EventSource,
    EventType,
    VeyraEvent,
    VeyraTaskPacket,
)
from runtime.agent_capability_directory import (  # noqa: E402
    AgentCapabilityDirectory,
    AgentCapabilitySelection,
    AgentCapabilitySelectionError,
    PHASE6_EXECUTION_PROFILE,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


class FakeOpenClawAdapter(AgentAdapter):
    trusted_native_provider_adapter = True

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.force_refreshes: list[bool] = []

    def connection_status(
        self, *, force_refresh: bool = False
    ) -> dict[str, Any]:
        self.force_refreshes.append(force_refresh)
        features = {
            "structured_task_packet": True,
            "rendered_prompt_fallback": True,
            "task_status": True,
            "stop_task": True,
            "agent_dialogue_v1": True,
            "caller_supplied_run_id": True,
            "idempotent_submit": True,
            "exact_stop": True,
            "tool_proxy_enforced": True,
            "tool_proxy_identity_match": True,
            "tool_proxy_enforcement_scope": (
                "veyra_governed_openclaw_sessions"
            ),
            "governance_callbacks_complete": True,
            "enforced_execution_profile": "phase3_sandbox_proposal",
            "enforced_execution_profiles": [
                "phase3_sandbox_proposal",
                PHASE6_EXECUTION_PROFILE,
            ],
        }
        return {
            "name": "openclaw",
            "status": "available",
            "connected": True,
            "features": features,
            "provider_certification": {
                "certification_status": "validated",
                "validated": True,
                "observed_at": "2026-07-29T00:00:00+00:00",
                "freshness": {"status": "fresh", "age_seconds": 0},
                "issues": [],
            },
        }

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        self.sent.append(task_packet.task_id)
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor="openclaw",
            status="submitted",
            result="submitted",
        )


class FakeGenericAdapter(AgentAdapter):
    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        raise AssertionError("generic adapter must never receive dispatch")

    def connection_status(self) -> dict[str, Any]:
        return {
            "name": "custom",
            "status": "available",
            "connected": True,
            "features": {
                "agent_dialogue_v1": True,
                "caller_supplied_run_id": True,
                "idempotent_submit": True,
                "exact_stop": True,
                "tool_proxy_enforced": True,
                "tool_proxy_identity_match": True,
                "tool_proxy_enforcement_scope": (
                    "veyra_governed_openclaw_sessions"
                ),
                "governance_callbacks_complete": True,
                "enforced_execution_profiles": [
                    PHASE6_EXECUTION_PROFILE
                ],
            },
            "provider_certification": {
                "certification_status": "validated",
                "validated": True,
                "freshness": {"status": "fresh", "age_seconds": 0},
                "issues": [],
            },
        }


class FakeRegistry:
    def __init__(
        self,
        config: dict[str, Any],
        adapters: dict[str, AgentAdapter],
    ) -> None:
        self.current_config = config
        self.adapters = adapters

    def config(self) -> dict[str, Any]:
        return self.current_config

    def names(self) -> list[str]:
        return sorted(self.adapters)

    def get(self, name: str) -> AgentAdapter:
        try:
            return self.adapters[name]
        except KeyError as exc:
            raise KeyError(name) from exc


def packet(
    store: WorldStateStore,
    selection: AgentCapabilitySelection,
) -> VeyraTaskPacket:
    now = datetime.now(timezone.utc)
    collaboration_binding = build_collaboration_binding(
        participant_id="phase6-catalog-primary",
        role="primary_analyst",
        parent_participant_id=None,
        handoff_index=0,
        capability_scope=[],
        evidence_scope=[],
        provider=selection.provider_binding,
        budget={
            "remaining_agent_calls": 1,
            "remaining_handoffs": 0,
            "remaining_evidence_patches": 0,
            "max_wall_time_seconds": 60,
            "max_context_bytes": 4096,
            "max_output_bytes": 8192,
        },
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
    )
    workspace_id = "phase6-catalog-workspace"
    dialogue = build_task_request(
        case_id="phase6-catalog-case",
        case_revision=1,
        turn_index=1,
        message_id="phase6-catalog-message",
        task_packet_id="phase6-catalog-dispatch",
        operation_id="phase6-catalog-operation",
        scope_digest=scope_digest(
            user_id="phase6-catalog-user",
            workspace_id=workspace_id,
        ),
        user_goal="analyze only",
        context={},
        collaboration_binding=collaboration_binding,
    )
    event = VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(
            channel="api",
            user_id="phase6-catalog-user",
            session_id="phase6-user-session",
        ),
        payload={"text": "analyze only"},
        event_id="phase6-catalog-event",
    )
    return TaskPacketBuilder(store).build(
        event=event,
        target_agent="openclaw",
        context_patch={},
        persona_patch={},
        policy_patch={},
        task_id="phase6-catalog-dispatch",
        case_id="phase6-catalog-case",
        step_id="phase6-catalog-step",
        runtime_run_id="veyra-p6-catalog-dispatch",
        dialogue_message=dialogue,
        execution_profile=PHASE6_EXECUTION_PROFILE,
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-phase6-catalog-") as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.patch_json(
            "local_world.json",
            {"current_project": "phase6-catalog-workspace"},
        )
        native = FakeOpenClawAdapter()
        generic = FakeGenericAdapter()
        config = {
            "selected_agent": "openclaw",
            "agents": {
                "openclaw": {
                    "kind": "openclaw",
                    "enabled": True,
                    "base_url": "ws://127.0.0.1:18789",
                },
                "custom": {
                    "kind": "custom",
                    "enabled": True,
                    "base_url": "http://127.0.0.1:9999",
                },
            },
        }
        registry = FakeRegistry(
            config,
            {"openclaw": native, "custom": generic},
        )
        directory = AgentCapabilityDirectory(
            state_store=store,
            registry=registry,
        )

        snapshot = directory.snapshot()
        rows = {
            item["runtime"]: item for item in snapshot["runtimes"]
        }
        expect(
            snapshot["eligible_runtimes"] == ["openclaw"]
            and rows["openclaw"]["collaboration_dispatch_eligible"]
            is True
            and rows["custom"]["diagnostic_only"] is True,
            "only the exact native operator-selected runtime is eligible",
            snapshot,
        )
        expect(
            snapshot["automatic_selection_allowed"] is False
            and snapshot["provider_switch_allowed"] is False
            and snapshot["topology"]
            == "single_runtime_multi_participant",
            "directory cannot select, switch, or overclaim multi-provider",
            snapshot,
        )
        expect(
            native.force_refreshes
            and all(native.force_refreshes),
            "native eligibility uses a fresh exact status observation",
            native.force_refreshes,
        )

        selection = directory.select_exact("openclaw")
        selected_packet = packet(store, selection)
        result = directory.dispatch(selection, selected_packet)
        expect(
            result.status == "submitted"
            and native.sent == ["phase6-catalog-dispatch"],
            "current exact selection dispatches once",
            result.to_dict(),
        )

        try:
            directory.select_exact("custom")
        except AgentCapabilitySelectionError as exc:
            custom_reason = exc.reason
        else:
            custom_reason = ""
        expect(
            custom_reason == "runtime_is_not_operator_selected",
            "non-selected runtime fails without fallback",
            custom_reason,
        )

        stale_selection = directory.select_exact("openclaw")
        registry.current_config = {
            **config,
            "agents": {
                **config["agents"],
                "openclaw": {
                    **config["agents"]["openclaw"],
                    "timeout": 99,
                },
            },
        }
        try:
            directory.dispatch(
                stale_selection,
                packet(store, stale_selection),
            )
        except AgentCapabilitySelectionError as exc:
            stale_reason = exc.reason
        else:
            stale_reason = ""
        expect(
            stale_reason == "runtime_configuration_changed"
            and native.sent == ["phase6-catalog-dispatch"],
            "configuration drift causes zero additional dispatch",
            {"reason": stale_reason, "sent": native.sent},
        )
        current_selection = directory.select_exact("openclaw")
        try:
            directory.dispatch(current_selection, selected_packet)
        except AgentCapabilitySelectionError as exc:
            binding_reason = exc.reason
        else:
            binding_reason = ""
        expect(
            binding_reason == "collaboration_provider_binding_changed"
            and native.sent == ["phase6-catalog-dispatch"],
            (
                "a newly selected provider cannot dispatch a packet bound "
                "to the prior provider identity"
            ),
            {"reason": binding_reason, "sent": native.sent},
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
