from __future__ import annotations

import json
from typing import Any

from core.definitions import RiskLevel, normalize_risk
from core.model_client import CoreModelClient, _env_value, redact_sensitive
from core.turn_context_builder import TurnContextBuilder
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent


CORE_DECISION_SYSTEM = (
    "You are Veyra Core's understanding-informed awareness planner, not an external agent runtime. "
    "Return strict JSON only. Veyra is an Awareness-driven Cognition and Governance Runtime. "
    "Your job is to preserve user understanding, reason about evidence, and produce structured decision output; you do not execute tools. "
    "First assess or reuse what the user really needs, what current state is known, what state may be stale, "
    "what evidence gap remains, and what would change the answer. Then choose a route. "
    "Use the minimal turn context and available_capabilities. Do not claim unavailable capabilities and do not "
    "answer from stale claims. direct_answer is allowed only when no fresh local/runtime/external/file/attachment "
    "evidence is needed, risk is low, and confidence is high enough to be useful. "
    "If the user asks about now/latest/status/running/logs/files/attachments/results/completion, require evidence "
    "unless the turn context supplies fresh evidence. Choose probe when a concrete read-only observation can close "
    "the gap; choose ask_user only when the missing input is user preference, permission, or unavailable context. "
    "Use agent when the task benefits from deeper reasoning, multi-step synthesis, external search, workspace/code/"
    "browser/debugging operations, or when Veyra's direct confidence is insufficient while policy allows delegation. "
    "Agent is governed by Veyra and returns a proposal or bounded result; it is not final authority. "
    "You cannot approve execution, lower risk, bypass Guardian, or ignore allowed_routes."
)

CORE_ANSWER_SYSTEM = (
    "You are Veyra Core's user reply composer. Reply naturally in the user's language, using the supplied "
    "decision, evidence, memory, and turn context. Return strict JSON only, but draft_response must be final "
    "text that can be sent to the user. Do not leak internal JSON, route labels, policy patches, or task packet "
    "details unless the user asks for internals. Start with the useful conclusion, then give the reason or next "
    "step when needed. Do not recite architecture slogans unless the user asks about architecture. "
    "For volatile facts, use only supplied fresh evidence; if evidence is missing or stale, say exactly what is "
    "missing and do not pretend to know. If an agent proposal is supplied, translate it into a human-readable "
    "answer while preserving Veyra policy, risk, confirmation, and verification boundaries. "
    "If the user is frustrated or confused, acknowledge the issue briefly and then give an actionable conclusion. "
    "If an image/attachment is referenced but only an attachment placeholder is available, say that Veyra has not "
    "received readable image content and ask for OCR/description or enabled vision intake."
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
        risk = str(rule_context.get("risk_level") or "R0")
        if risk == RiskLevel.R5.value:
            return False
        if status.get("decision_mode") == "always":
            return True
        if kind == "decision":
            return True
        if kind in {"perception", "agency", "memory", "external_world"}:
            return True
        if kind in {"answer", "probe_answer"}:
            return True
        route = str(rule_context.get("route") or "")
        complexity = str(rule_context.get("complexity") or "simple")
        intent = str(rule_context.get("intent") or "unknown")
        if kind == "foresight" and route in {"direct_answer", "probe", "ask_user"} and risk in {"R0", "R1"} and complexity == "simple":
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
        model_assist = rule_decision.get("model_assist") if isinstance(rule_decision.get("model_assist"), dict) else {}
        payload = {
            "user_message": text,
            "attention_focus": attention_focus,
            "turn_understanding": model_assist.get("turn_understanding", {}),
            "rule_decision": redact_sensitive(rule_decision),
            "turn_context": turn_context,
            "allowed_routes": ["direct_answer", "probe", "skill", "agent", "ask_user", "human_review", "block"],
            "required_json_fields": {
                "situation_assessment": {
                    "user_goal": "explicit user goal",
                    "what_user_really_needs": "practical need behind the request",
                    "known_state": "list",
                    "missing_state": "list",
                    "freshness_need": "none|local|runtime|external|file|attachment|memory",
                    "risk": "R0-R5",
                    "confidence": "0.0-1.0",
                },
                "route_decision": {
                    "route": "direct_answer|probe|skill|agent|ask_user|human_review|block",
                    "why_this_route": "short rationale",
                    "why_not_other_routes": "list",
                },
                "intent": "conversation|information|action|implementation|unknown",
                "complexity": "simple|moderate|complex",
                "risk_level": "R0-R5",
                "freshness_required": "boolean",
                "needs_probe": "boolean",
                "needs_agent": "boolean",
                "needs_user_confirmation": "boolean",
                "memory_policy": "forget|short_term|long_term",
                "reasoning_mode": "direct|evidence|execution",
                "recommended_route": "direct_answer|probe|skill|agent|ask_user|human_review|block",
                "required_capabilities": "list of capability ids from available_capabilities.capabilities",
                "capability_request": {
                    "capability": "capability id if one is needed",
                    "probe": "probe name only when recommended_route=probe",
                    "skill": "skill name only when recommended_route=skill",
                    "reason": "short rationale",
                },
                "draft_response": "natural reply when direct_answer or ask_user is appropriate",
                "context_gaps": "list of missing context/evidence",
                "reply_strategy": {
                    "tone": "natural|direct|technical|supportive",
                    "must_include": "list",
                    "must_avoid": "list",
                },
                "reason": "short rationale",
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
                "used_sources": "list of evidence, memory, or decision inputs used",
                "memory_policy": "none|read|write_candidate|forget|short_term|long_term",
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
                "memory_policy": "forget|short_term|long_term",
            },
        }
        result = self.client.complete_json(
            purpose="probe_answer",
            system=(
                CORE_ANSWER_SYSTEM
                + " Use only supplied probe evidence for volatile facts. If probe evidence is insufficient, do not fill gaps "
                "from memory or model guesses. Give the conclusion first, then evidence and uncertainty. Avoid machine phrasing "
                "like 'according to probe_result' unless the user asked for technical details."
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
                "Return claims only when they are directly grounded in the probe evidence. "
                "Every claim must include observed_at when available, ttl_seconds, source, confidence, and claim_type "
                "fact|fault|capability_limit. For failed probes, output an anomaly instead of pretending no result exists. "
                "Do not cache failed observations as durable facts."
            ),
        }
        result = self.client.complete_json(
            purpose="perception",
            system=(
                "You are Veyra Core's perception interpreter. Return strict JSON with optional "
                "summary, anomaly {kind,next_action}, and claims [{key, claim, observed_at, ttl_seconds, source, "
                "confidence, claim_type}]. Distinguish fact claims, fault claims, and capability-limit claims. "
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
            original = config.get("core_model") if isinstance(config.get("core_model"), dict) else {}
            api_key_env = str(original.get("api_key_env") or "VEYRA_CORE_MODEL_API_KEY")
            core_model["api_key_set"] = bool(original.get("api_key") or _env_value(api_key_env, ""))
            core_model["api_key"] = "<redacted>" if original.get("api_key") else ""
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
