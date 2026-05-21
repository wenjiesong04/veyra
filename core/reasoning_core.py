from __future__ import annotations

import json
from typing import Any

from core.definitions import RiskLevel, normalize_risk
from core.model_client import CoreModelClient, redact_sensitive
from core.world_state import WorldStateStore


class CoreReasoning:
    """Model-assisted cognition for Veyra Core.

    The model proposes understanding and plans. Veyra's deterministic risk
    policy, Foresight, Guardian, and Tool Proxy remain the execution boundary.
    """

    def __init__(self, state_store: WorldStateStore, client: CoreModelClient | None = None) -> None:
        self.state_store = state_store
        self.client = client or CoreModelClient(state_store)

    def status(self) -> dict[str, Any]:
        return self.client.status()

    def is_enabled(self) -> bool:
        return bool(self.status().get("configured"))

    def should_assist(self, kind: str, rule_context: dict[str, Any]) -> bool:
        status = self.status()
        if not status.get("configured"):
            return False
        if status.get("decision_mode") == "always":
            return True
        if kind in {"perception", "agency"}:
            return True
        route = str(rule_context.get("route") or "")
        risk = str(rule_context.get("risk_level") or "R0")
        complexity = str(rule_context.get("complexity") or "simple")
        intent = str(rule_context.get("intent") or "unknown")
        if risk == RiskLevel.R5.value:
            return False
        return route in {"agent", "human_review"} or risk in {"R2", "R3", "R4"} or complexity != "simple" or intent == "unknown"

    def decision_assist(self, *, text: str, attention_focus: list[str], rule_decision: dict[str, Any]) -> dict[str, Any]:
        if not self.should_assist("decision", rule_decision):
            return {"status": "skipped"}
        state = self._decision_state_snapshot(attention_focus)
        payload = {
            "user_message": text,
            "attention_focus": attention_focus,
            "rule_decision": redact_sensitive(rule_decision),
            "state_snapshot": state,
            "allowed_routes": ["direct_answer", "probe", "skill", "agent", "human_review", "block"],
            "known_probes": ["system", "git", "port", "process", "file", "log", "network", "web", "openclaw", "hermes", "mcp"],
            "known_skills": ["diagnose_openclaw", "check_port", "summarize_logs", "safe_git_commit"],
        }
        result = self.client.complete_json(
            purpose="decision",
            system=(
                "You are Veyra Core's internal reasoning layer. Return strict JSON only. "
                "You may improve intent, complexity, route, selected_probe, selected_skill, "
                "solution_outline, and agent_context. You cannot approve execution, lower risk, "
                "or bypass Guardian. Prefer delegating complex implementation to the selected agent "
                "with concise context and a proposed solution outline."
            ),
            user=json.dumps(payload, ensure_ascii=False),
        )
        self._trace("decision", result, {"route": rule_decision.get("route"), "risk_level": rule_decision.get("risk_level")})
        return result

    def perception_assist(self, probe_result: dict[str, Any]) -> dict[str, Any]:
        if not self.should_assist("perception", {"route": "probe", "risk_level": "R1"}):
            return {"status": "skipped"}
        payload = {
            "probe_result": redact_sensitive(probe_result, max_string=1800),
            "task": (
                "Interpret the probe result into concise operational meaning. "
                "Return claims only when they are directly grounded in the probe evidence."
            ),
        }
        result = self.client.complete_json(
            purpose="perception",
            system=(
                "You are Veyra Core's perception interpreter. Return strict JSON with optional "
                "summary, anomaly {kind,next_action}, and claims [{key, claim, confidence, ttl_seconds}]. "
                "Do not invent facts beyond the probe evidence."
            ),
            user=json.dumps(payload, ensure_ascii=False),
        )
        self._trace("perception", result, {"probe": probe_result.get("probe"), "status": probe_result.get("status")})
        return result

    def agency_assist(self, *, goals: dict[str, Any], world_state: dict[str, Any], rule_gaps: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.should_assist("agency", {"route": "agent", "risk_level": "R1"}):
            return {"status": "skipped"}
        payload = {
            "goals": redact_sensitive(goals),
            "world_state": self._agency_state_snapshot(world_state),
            "rule_gaps": redact_sensitive(rule_gaps),
            "risk_policy": "R1 may be executed read-only; R2 may be suggested; R3+ requires review; R5 is blocked.",
        }
        result = self.client.complete_json(
            purpose="agency",
            system=(
                "You are Veyra Core's proactive agency layer. Return strict JSON with "
                "state_gaps [{gap_id,target,observed_status,risk_level,suggested_action,action_text,confidence}]. "
                "Suggest bounded maintenance intentions that preserve user control."
            ),
            user=json.dumps(payload, ensure_ascii=False),
        )
        self._trace("agency", result, {"rule_gap_count": len(rule_gaps)})
        return result

    def _decision_state_snapshot(self, attention_focus: list[str]) -> dict[str, Any]:
        state = self.state_store.read_all()
        belief = state.get("belief_state", {}) if isinstance(state.get("belief_state"), dict) else {}
        claims = belief.get("claims", []) if isinstance(belief.get("claims"), list) else []
        recent_claims = [
            {
                "key": claim.get("key"),
                "claim": claim.get("claim"),
                "source": claim.get("source"),
                "status": claim.get("status"),
                "confidence": claim.get("confidence"),
            }
            for claim in claims[-12:]
            if isinstance(claim, dict)
        ]
        return redact_sensitive(
            {
                "executor_state": state.get("executor_state", {}),
                "risk_state": state.get("risk_state", {}),
                "belief_summary": belief.get("summary", {}),
                "recent_claims": recent_claims,
                "attention_focus": attention_focus,
                "agent_config": self._public_agent_config(state.get("agent_config", {})),
            }
        )

    def _agency_state_snapshot(self, world_state: dict[str, Any]) -> dict[str, Any]:
        belief = world_state.get("belief_state", {}) if isinstance(world_state.get("belief_state"), dict) else {}
        local_world = world_state.get("local_world", {}) if isinstance(world_state.get("local_world"), dict) else {}
        probes = local_world.get("probes", {}) if isinstance(local_world.get("probes"), dict) else {}
        compact_probes = {
            name: {
                "status": value.get("status"),
                "summary": value.get("summary"),
                "anomaly": value.get("anomaly"),
                "observed_at": value.get("observed_at") or value.get("timestamp"),
            }
            for name, value in probes.items()
            if isinstance(value, dict)
        }
        return redact_sensitive(
            {
                "executor_state": world_state.get("executor_state", {}),
                "risk_state": world_state.get("risk_state", {}),
                "belief_summary": belief.get("summary", {}),
                "local_probes": compact_probes,
                "task_state": world_state.get("task_state", {}),
            }
        )

    def _public_agent_config(self, config: Any) -> dict[str, Any]:
        if not isinstance(config, dict):
            return {}
        public = redact_sensitive(config)
        core_model = public.get("core_model") if isinstance(public.get("core_model"), dict) else {}
        if core_model:
            core_model["api_key_set"] = bool(core_model.get("api_key"))
            core_model["api_key"] = "<redacted>" if core_model.get("api_key") else ""
        return public

    def _trace(self, purpose: str, result: dict[str, Any], request_summary: dict[str, Any]) -> None:
        self.state_store.append_jsonl(
            "core_model_trace.jsonl",
            {
                "purpose": purpose,
                "status": result.get("status"),
                "request_summary": redact_sensitive(request_summary),
                "result": redact_sensitive(result, max_string=1600),
            },
        )


def safe_model_risk(value: Any, default: RiskLevel = RiskLevel.R0) -> RiskLevel:
    try:
        return normalize_risk(str(value))
    except (TypeError, ValueError):
        return default
