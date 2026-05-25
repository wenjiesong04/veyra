from __future__ import annotations

from typing import Any

from core.definitions import lifecycle_statuses, operational_modes, risk_catalog


ARCHITECTURE_BLOCKS: list[dict[str, Any]] = [
    {
        "id": "core",
        "name": "Veyra Core",
        "role": "Awareness, active runtime loop, state, attention, model-assisted reasoning, decision, risk governance, verification, and patch generation.",
        "paths": ["core/", "awareness/", "decision/", "foresight/", "guardian/"],
        "status": "implemented",
        "validation_state": "local_self_tested",
    },
    {
        "id": "interface_adapter",
        "name": "Interface Adapter / Agent Adapter",
        "role": "Normalize multi-channel inbound events and connect one or more validated Agent runtimes.",
        "paths": ["interface/"],
        "status": "implemented",
        "validation_state": "external_runtime_validation_pending",
    },
    {
        "id": "probes",
        "name": "Probe Tools",
        "role": "Run low-risk read-only checks for system, git, port, process, files, logs, and runtime status.",
        "paths": ["probes/"],
        "status": "implemented",
        "validation_state": "environment_dependent",
    },
    {
        "id": "memory_bridge",
        "name": "Memory Bridge",
        "role": "Read useful long-term memory summaries and write filtered task learnings.",
        "paths": ["memory_bridge/"],
        "status": "implemented",
        "validation_state": "external_provider_validation_pending",
    },
    {
        "id": "skills",
        "name": "Skill",
        "role": "Execute fixed, low-risk workflows that do not need a full Agent planner.",
        "paths": ["skills/"],
        "status": "implemented",
        "validation_state": "local_self_tested",
    },
    {
        "id": "tool_proxy",
        "name": "Tool Proxy",
        "role": "Interpose Guardian policy before shell, file, browser, and API operations.",
        "paths": ["tool_proxy/"],
        "status": "implemented",
        "validation_state": "executor_hooks_configurable",
    },
    {
        "id": "rollback_audit",
        "name": "Rollback / Audit",
        "role": "Record traces, snapshots, diffs, rollback actions, replayable execution evidence, and automatic compensation review jobs.",
        "paths": ["rollback_audit/", "state/*.jsonl", "state/snapshots/"],
        "status": "implemented",
        "validation_state": "local_self_tested",
    },
    {
        "id": "web_control_ui",
        "name": "Web Control UI",
        "role": "Expose awareness, runtime, action review, tool proxy, and rollback state to operators.",
        "paths": ["web/", "ui/"],
        "status": "implemented",
        "validation_state": "build_verified",
    },
]


CORE_MODULES: list[dict[str, str]] = [
    {"id": "runtime_entity", "path": "core/runtime_entity.py", "status": "p8_continuous_entity"},
    {"id": "awareness_loop", "path": "core/awareness_loop.py", "status": "p8_continuous_entity"},
    {"id": "active_runtime_loop", "path": "runtime/active_loop.py", "status": "p8_continuous_entity"},
    {"id": "attention_core", "path": "awareness/attention_core.py", "status": "mvp_foundation"},
    {"id": "belief_uncertainty_core", "path": "awareness/belief_core.py", "status": "p8_deep_ttl"},
    {"id": "world_state", "path": "core/world_state.py", "status": "mvp_foundation"},
    {"id": "core_model_client", "path": "core/model_client.py", "status": "p6_model_assisted"},
    {"id": "core_reasoning", "path": "core/reasoning_core.py", "status": "p6_model_assisted"},
    {"id": "agency_core", "path": "core/agency_core.py", "status": "p8_proactive_runtime"},
    {"id": "perception_layer", "path": "core/perception_layer.py", "status": "p6_model_assisted"},
    {"id": "persona_engine", "path": "core/persona_engine.py", "status": "mvp_foundation"},
    {"id": "decision_core", "path": "core/decision_core.py", "status": "p6_model_assisted"},
    {"id": "foresight_engine", "path": "core/foresight_engine.py", "status": "mvp_foundation"},
    {"id": "guardian_execution_controller", "path": "core/guardian_controller.py", "status": "mvp_foundation"},
    {"id": "verifier", "path": "core/verifier.py", "status": "p4_completed"},
    {"id": "context_patch_builder", "path": "core/context_patch_builder.py", "status": "mvp_foundation"},
    {"id": "agent_orchestrator", "path": "runtime/agent_orchestrator.py", "status": "p8_validated_multi_agent"},
    {"id": "replay_runtime", "path": "rollback_audit/replay_runtime.py", "status": "p8_auto_replay"},
]


STATE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "user_world",
        "file": "state/user_world.json",
        "owner": "WorldState.UserWorld",
        "purpose": "Current user goal, preferences, working style, and relevant user context cache.",
        "freshness": "task_scoped",
    },
    {
        "id": "local_world",
        "file": "state/local_world.json",
        "owner": "WorldState.LocalWorld",
        "purpose": "Current project, probes, runtime facts, and local execution environment.",
        "freshness": "ttl_probe_backed",
    },
    {
        "id": "external_world",
        "file": "state/external_world.json",
        "owner": "WorldState.ExternalWorld",
        "purpose": "Watchlist and external context summaries relevant to current goals.",
        "freshness": "ttl_source_backed",
    },
    {
        "id": "risk_state",
        "file": "state/risk_state.json",
        "owner": "Guardian",
        "purpose": "Current risk level, risk catalog, and active risk signals.",
        "freshness": "event_scoped",
    },
    {
        "id": "belief_state",
        "file": "state/belief_state.json",
        "owner": "BeliefCore",
        "purpose": "Claims with confidence, source, TTL, and stale/conflict status.",
        "freshness": "ttl_claim_backed",
    },
    {
        "id": "task_state",
        "file": "state/task_state.json",
        "owner": "AwarenessLoop",
        "purpose": "Current task, route, status, and recent task history.",
        "freshness": "event_scoped",
    },
    {
        "id": "attention_state",
        "file": "state/attention_state.json",
        "owner": "AttentionCore",
        "purpose": "Current focus slice, ignored noise, and context scope hints.",
        "freshness": "event_scoped",
    },
    {
        "id": "executor_state",
        "file": "state/executor_state.json",
        "owner": "AgentAdapter",
        "purpose": "Selected Agent Runtime, connection status, and adapter capability status.",
        "freshness": "probe_backed",
    },
    {
        "id": "persona_state",
        "file": "state/persona_state.json",
        "owner": "PersonaEngine",
        "purpose": "Active persona modes and channel/risk/Agent binding decisions for recent events.",
        "freshness": "event_scoped",
    },
    {
        "id": "channel_state",
        "file": "state/channel_state.json",
        "owner": "Interface.ChannelRouter",
        "purpose": "Multi-channel intake sessions, dedupe records, and local outbox delivery records.",
        "freshness": "append_only_runtime",
    },
    {
        "id": "active_loop_state",
        "file": "state/active_loop_state.json",
        "owner": "Runtime.ActiveRuntimeLoop",
        "purpose": "Continuous awareness loop lifecycle, schedule, recent ticks, and step outcomes.",
        "freshness": "scheduled_runtime",
    },
    {
        "id": "replay_runtime_state",
        "file": "state/replay_runtime_state.json",
        "owner": "RollbackAudit.ReplayRuntime",
        "purpose": "Automatic replay/compensation candidates, review job state, and scan timestamps.",
        "freshness": "audit_derived",
    },
    {
        "id": "execution_trace",
        "file": "state/execution_trace.jsonl",
        "owner": "RollbackAudit.ExecutionTrace",
        "purpose": "Execution evidence linking events, routes, executors, results, and verifier verdicts.",
        "freshness": "append_only_audit",
    },
    {
        "id": "core_model_trace",
        "file": "state/core_model_trace.jsonl",
        "owner": "VeyraCore.CoreReasoning",
        "purpose": "Redacted evidence of Core model-assisted decision, perception, and agency reasoning.",
        "freshness": "append_only_audit",
    },
]


IMPLEMENTATION_PHASES: list[dict[str, str]] = [
    {"phase": "P0", "name": "Foundation definitions", "status": "completed", "validation_state": "local_self_tested"},
    {"phase": "P1", "name": "State and probe hardening", "status": "completed", "validation_state": "local_self_tested"},
    {"phase": "P2", "name": "Decision, Guardian, and Tool Proxy policy depth", "status": "completed", "validation_state": "local_self_tested"},
    {"phase": "P3", "name": "Agent adapter execution contracts", "status": "completed", "validation_state": "contract_self_tested"},
    {"phase": "P4", "name": "Rollback, audit, and verifier depth", "status": "completed", "validation_state": "local_self_tested"},
    {"phase": "P5", "name": "Web Control Console completeness", "status": "completed", "validation_state": "build_verified"},
    {"phase": "P6", "name": "End-to-end runtime hardening", "status": "implemented", "validation_state": "live_runtime_validation_pending"},
    {"phase": "P7", "name": "Production operations and safety validation", "status": "implemented", "validation_state": "production_soak_validation_pending"},
    {"phase": "P8", "name": "Continuous awareness entity runtime", "status": "implemented", "validation_state": "bounded_local_self_tested"},
]


def architecture_snapshot() -> dict[str, Any]:
    return {
        "blocks": ARCHITECTURE_BLOCKS,
        "core_modules": CORE_MODULES,
        "state_definitions": STATE_DEFINITIONS,
        "risk_levels": risk_catalog(),
        "lifecycle_statuses": lifecycle_statuses(),
        "operational_modes": operational_modes(),
        "implementation_phases": IMPLEMENTATION_PHASES,
    }
