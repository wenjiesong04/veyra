from __future__ import annotations

import json
from typing import Any

from core.definitions import RiskLevel, normalize_risk
from core.model_client import CoreModelClient, redact_sensitive
from core.turn_context_builder import TurnContextBuilder
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent


CORE_DECISION_SYSTEM = (
    "You are Veyra Core's internal cognition layer, not an external agent runtime. "
    "Return strict JSON only. Veyra is an Awareness Entity: it uses current state, "
    "belief freshness, attention focus, risk policy, probes, skills, and selected Agent Runtime "
    "to decide how to answer or solve the user's message. "
    "Do not answer from stale claims. If fresh local, external, time, status, or attachment evidence "
    "is required, choose route=probe or ask a focused clarification in draft_response. "
    "Use route=agent only for implementation, multi-step execution, complex debugging, or capabilities "
    "that genuinely need the selected Agent Runtime. Ordinary chat and explanations should stay "
    "route=direct_answer with a natural draft_response. Probe tools collect evidence; do not expose "
    "raw internal probe summaries as the final conversational style. "
    "You cannot approve execution, lower risk, bypass Guardian, or ignore allowed_routes."
)

CORE_ANSWER_SYSTEM = (
    "You are Veyra Core's answer composer. Reply naturally in the user's language, using the supplied "
    "turn context. Do not recite architecture slogans unless the user asks about architecture. "
    "If the user asks about current time/date/status/latest facts and no evidence is supplied, say what "
    "fresh observation is needed instead of guessing. If an image/attachment is referenced but only an "
    "attachment placeholder is available, say that Veyra has not received readable image content and ask "
    "for OCR/description or enabled vision intake. Return strict JSON only."
)


class CoreReasoning:
    """Model-assisted cognition for Veyra Core.

    The model proposes understanding and plans. Veyra's deterministic risk
    policy, Foresight, Guardian, and Tool Proxy remain the execution boundary.
    """

    def __init__(self, state_store: WorldStateStore, client: CoreModelClient | None = None) -> None:
        self.state_store = state_store
        self.client = client or CoreModelClient(state_store)
        self.turn_context = TurnContextBuilder(state_store)

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
        if kind in {"perception", "agency", "memory", "external_world"}:
            return True
        if kind in {"answer", "probe_answer"}:
            return True
        route = str(rule_context.get("route") or "")
        risk = str(rule_context.get("risk_level") or "R0")
        complexity = str(rule_context.get("complexity") or "simple")
        intent = str(rule_context.get("intent") or "unknown")
        selected_probe = str(rule_context.get("selected_probe") or "")
        if risk == RiskLevel.R5.value:
            return False
        if kind == "foresight" and route in {"direct_answer", "probe"} and risk in {"R0", "R1"} and complexity == "simple":
            return False
        if kind == "decision" and route == "probe" and selected_probe in {"time", "port", "process", "git", "system", "openclaw", "hermes", "mcp", "network"}:
            return False
        if route == "direct_answer" and intent in {"information", "unknown"}:
            return True
        return route in {"agent", "human_review"} or risk in {"R2", "R3", "R4"} or complexity != "simple" or intent == "unknown"

    def decision_assist(
        self,
        *,
        text: str,
        attention_focus: list[str],
        rule_decision: dict[str, Any],
        event: VeyraEvent | None = None,
    ) -> dict[str, Any]:
        if not self.should_assist("decision", rule_decision):
            return {"status": "skipped"}
        turn_context = self.turn_context.build(user_message=text, attention_focus=attention_focus, event=event, rule_decision=rule_decision)
        payload = {
            "user_message": text,
            "attention_focus": attention_focus,
            "rule_decision": redact_sensitive(rule_decision),
            "turn_context": turn_context,
            "allowed_routes": ["direct_answer", "probe", "skill", "agent", "human_review", "block"],
            "known_probes": ["time", "system", "git", "port", "process", "file", "log", "network", "web", "openclaw", "hermes", "mcp"],
            "known_skills": ["diagnose_openclaw", "check_port", "summarize_logs", "safe_git_commit"],
            "required_json_fields": {
                "route": "direct_answer|probe|skill|agent|human_review|block",
                "risk_level": "R0-R5",
                "intent": "information|action|implementation|conversation|unknown",
                "complexity": "simple|moderate|complex",
                "reason": "short rationale",
                "selected_probe": "only when route=probe",
                "draft_response": "natural reply when route=direct_answer or clarification is needed",
                "needs_observation": "boolean",
                "context_gaps": "list of missing context/evidence",
                "memory_policy": {"mode": "ignore|session|long_term", "reason": "why"},
                "solution_outline": "list for agent/skill plans",
                "agent_context": "object for agent handoff",
            },
        }
        result = self.client.complete_json(
            purpose="decision",
            system=CORE_DECISION_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
        )
        self._trace("decision", result, {"route": rule_decision.get("route"), "risk_level": rule_decision.get("risk_level")})
        return result

    def answer_assist(
        self,
        *,
        text: str,
        attention_focus: list[str],
        decision: dict[str, Any],
        event: VeyraEvent | None = None,
    ) -> dict[str, Any]:
        if not self.should_assist("answer", decision):
            return {"status": "skipped"}
        payload = {
            "user_message": text,
            "decision": redact_sensitive(decision),
            "turn_context": self.turn_context.build(user_message=text, attention_focus=attention_focus, event=event, rule_decision=decision),
            "required_json_fields": {
                "draft_response": "final user-facing reply",
                "confidence": "0.0-1.0",
                "memory_policy": {"mode": "ignore|session|long_term", "reason": "why"},
                "needs_observation": "boolean",
                "context_gaps": "list",
            },
        }
        result = self.client.complete_json(
            purpose="answer",
            system=CORE_ANSWER_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
        )
        self._trace("answer", result, {"route": decision.get("route"), "risk_level": decision.get("risk_level")})
        return result

    def probe_answer_assist(
        self,
        *,
        text: str,
        attention_focus: list[str],
        probe_result: dict[str, Any],
        decision: dict[str, Any],
        event: VeyraEvent | None = None,
    ) -> dict[str, Any]:
        if not self.should_assist("probe_answer", {"route": "probe", "risk_level": "R1", "intent": decision.get("intent", "")}):
            return {"status": "skipped"}
        payload = {
            "user_message": text,
            "decision": redact_sensitive(decision),
            "turn_context": self.turn_context.build(user_message=text, attention_focus=attention_focus, event=event, rule_decision=decision),
            "probe_result": redact_sensitive(probe_result, max_string=2400, max_list=8),
            "task": "Answer the user using the probe evidence and the turn context. Keep raw internals out of the final style.",
            "required_json_fields": {
                "draft_response": "final user-facing reply grounded in probe_result",
                "confidence": "0.0-1.0",
                "used_evidence": "list",
                "memory_policy": {"mode": "ignore|session|long_term", "reason": "why"},
            },
        }
        result = self.client.complete_json(
            purpose="probe_answer",
            system=(
                CORE_ANSWER_SYSTEM
                + " Use only supplied probe evidence for volatile facts. Mention uncertainty when evidence is missing or stale."
            ),
            user=json.dumps(payload, ensure_ascii=False),
        )
        self._trace("probe_answer", result, {"probe": probe_result.get("probe"), "status": probe_result.get("status")})
        return result

    def perception_assist(self, probe_result: dict[str, Any]) -> dict[str, Any]:
        details = probe_result.get("details") if isinstance(probe_result.get("details"), dict) else {}
        if (
            probe_result.get("model_assist") is False
            or details.get("model_assist") is False
            or details.get("perception_model_assist") is False
        ):
            return {"status": "skipped", "reason": "probe_result_has_direct_summary"}
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

    def foresight_assist(
        self,
        *,
        text: str,
        risk_level: str,
        rule_foresight: dict[str, Any],
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rule_context = {
            "route": (decision or {}).get("route", ""),
            "risk_level": risk_level,
            "complexity": (decision or {}).get("complexity", ""),
            "intent": (decision or {}).get("intent", ""),
        }
        if not self.should_assist("foresight", rule_context):
            return {"status": "skipped"}
        payload = {
            "user_message": text,
            "risk_level": risk_level,
            "decision": redact_sensitive(decision or {}),
            "rule_foresight": redact_sensitive(rule_foresight),
            "task": "Predict operational impact, reversibility, assumptions, required preconditions, and safer alternatives.",
        }
        result = self.client.complete_json(
            purpose="foresight",
            system=(
                "You are Veyra Core's foresight layer. Return strict JSON with optional "
                "impact_summary, reversible, side_effects, required_preconditions, unsafe_assumptions, "
                "safer_alternatives, and confidence. You may add caution but cannot approve execution "
                "or lower risk."
            ),
            user=json.dumps(payload, ensure_ascii=False),
        )
        self._trace("foresight", result, {"risk_level": risk_level, "route": rule_context.get("route")})
        return result

    def memory_assist(
        self,
        *,
        session_id: str,
        focus: list[str],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.should_assist("memory", {"route": "agent", "risk_level": "R1"}):
            return {"status": "skipped"}
        payload = {
            "session_id": session_id,
            "focus": focus,
            "candidates": redact_sensitive(candidates, max_string=900),
            "task": "Rank memory items by relevance to the current focus. Return only indexes from candidates.",
        }
        result = self.client.complete_json(
            purpose="memory",
            system=(
                "You are Veyra Core's memory relevance selector. Return strict JSON with "
                "selected_indexes [integer], relevance_notes, and optional summary. "
                "Select only memories useful for the current task. Do not include secrets or local paths."
            ),
            user=json.dumps(payload, ensure_ascii=False),
        )
        self._trace("memory", result, {"candidate_count": len(candidates), "focus": focus[:8]})
        return result

    def external_world_assist(
        self,
        *,
        target: str,
        probe_result: dict[str, Any],
        current_goal: str = "",
    ) -> dict[str, Any]:
        if not self.should_assist("external_world", {"route": "probe", "risk_level": "R1"}):
            return {"status": "skipped"}
        payload = {
            "target": target,
            "current_goal": current_goal,
            "probe_result": redact_sensitive(probe_result, max_string=1800),
            "task": "Summarize external world relevance and decide whether this target should keep being watched.",
        }
        result = self.client.complete_json(
            purpose="external_world",
            system=(
                "You are Veyra Core's ExternalWorld interpreter. Return strict JSON with "
                "summary, relevance, watch_recommendation keep|pause|remove, reasons, and optional claims. "
                "Ground all claims in the probe result."
            ),
            user=json.dumps(payload, ensure_ascii=False),
        )
        self._trace("external_world", result, {"target": target, "status": probe_result.get("status")})
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
                "current_time": TurnContextBuilder(self.state_store).build(user_message="", attention_focus=attention_focus).get("current_time", {}),
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
