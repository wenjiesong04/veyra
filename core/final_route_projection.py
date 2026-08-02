from __future__ import annotations

from dataclasses import fields, replace
from typing import Any

from core.decision_plan import DecisionPlan
from core.definitions import RiskLevel
from interface.event_schema import Decision, Route


RESPONSE_AUTHORITY_SCHEMA_VERSION = "veyra.response_authority.v1"
_NONEXECUTING_ROUTES = {Route.DIRECT_ANSWER, Route.ASK_USER}
_GOVERNANCE_ROUTES = {Route.BLOCK, Route.HUMAN_REVIEW, Route.ROLLBACK}
_EFFECTFUL_ROUTES = {
    Route.AGENT.value,
    Route.NATIVE_TOOL.value,
    Route.PROBE.value,
    Route.ROLLBACK.value,
    Route.SKILL.value,
}
_NONEXECUTING_CAPABILITIES = {
    "",
    "ask_user",
    "human_review",
    "native_answer",
    "unknown",
}
_NONEXECUTING_EXECUTION_TIERS = {"", "L0_direct_answer"}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _request_is_effectful(request: dict[str, Any]) -> bool:
    route = str(request.get("target_route") or "").strip()
    receiver = str(request.get("receiver_type") or "").strip()
    executor = str(request.get("executor") or "").strip()
    capability = str(
        request.get("capability_id") or request.get("capability") or ""
    ).strip()
    return bool(
        route in _EFFECTFUL_ROUTES
        or receiver in {"agent", "probe", "skill", "tool"}
        or executor
        or capability not in _NONEXECUTING_CAPABILITIES
        or request.get("probe")
        or request.get("skill")
        or request.get("tool")
    )


def decision_from_public_dict(payload: dict[str, Any]) -> Decision | None:
    """Restore a public Decision artifact for terminal consistency checks."""

    if not isinstance(payload, dict):
        return None
    try:
        values = {
            field.name: payload[field.name]
            for field in fields(Decision)
            if field.name in payload
        }
        values["route"] = Route(str(payload.get("route") or ""))
        values["risk_level"] = RiskLevel(
            str(payload.get("risk_level") or "")
        )
        values["reason"] = str(payload.get("reason") or "")
        return Decision(**values)
    except (TypeError, ValueError):
        return None


def has_effectful_candidate(decision: Decision) -> bool:
    """Return whether any structured layer proposed capability execution.

    The final outer route is insufficient because CognitionPipeline may already
    have narrowed an Agent/Probe/Skill proposal.  Inspect only structured
    provenance; user-facing prose never grants or detects authority here.
    """

    if (
        decision.route.value in _EFFECTFUL_ROUTES
        or decision.needs_probe
        or decision.needs_agent
        or decision.selected_probe
        or decision.target_agent
    ):
        return True
    if decision.reasoning_mode == "execution":
        return True
    if any(
        str(capability or "").strip() not in _NONEXECUTING_CAPABILITIES
        for capability in decision.required_capabilities
    ):
        return True
    if (
        str(decision.capability or "").strip()
        not in _NONEXECUTING_CAPABILITIES
    ):
        return True

    assist = _mapping(decision.model_assist)
    requests = [
        _mapping(decision.capability_request),
        _mapping(assist.get("capability_request")),
    ]
    if any(_request_is_effectful(request) for request in requests):
        return True
    route_decision = _mapping(assist.get("route_decision"))
    plan = _mapping(assist.get("decision_plan"))
    audit_hints = _mapping(plan.get("audit_hints"))
    proposed_routes = {
        str(assist.get("recommended_route") or "").strip(),
        str(route_decision.get("route") or "").strip(),
        str(audit_hints.get("route") or "").strip(),
    }
    execution_tiers = {
        str(assist.get("execution_tier") or "").strip(),
        str(plan.get("execution_tier") or "").strip(),
    }
    required_capabilities = [
        *(
            assist.get("required_capabilities")
            if isinstance(assist.get("required_capabilities"), list)
            else []
        ),
        *(
            plan.get("required_capabilities")
            if isinstance(plan.get("required_capabilities"), list)
            else []
        ),
    ]
    return bool(
        proposed_routes.intersection(_EFFECTFUL_ROUTES)
        or any(tier not in _NONEXECUTING_EXECUTION_TIERS for tier in execution_tiers)
        or str(assist.get("reasoning_mode") or "").strip() == "execution"
        or any(
            str(capability or "").strip() not in _NONEXECUTING_CAPABILITIES
            for capability in required_capabilities
        )
        or assist.get("agent_goal")
        or assist.get("probe_params")
        or plan.get("needs_probe")
        or plan.get("needs_agent")
        or plan.get("probe_requests")
        or plan.get("selected_agent")
    )


def project_nonexecuting_route(
    decision: Decision,
    *,
    route: Route,
    reason: str,
    original: Decision | None = None,
    signal: str = "policy:nonexecuting_route_projected",
) -> Decision:
    """Atomically project an execution candidate onto a non-executing route.

    Rejected proposal metadata remains only as a bounded audit summary.  It is
    removed from the final capability envelope, DecisionPlan, Foresight inputs,
    and response composer.  The server-owned response authority is deliberately
    not supplied by a model and carries no execution or delivery receipts.
    """

    if route not in _NONEXECUTING_ROUTES:
        raise ValueError(f"non-executing route required, got {route.value}")
    candidate = original or decision
    assist = dict(decision.model_assist or {})
    proposal = _proposal_summary(candidate)
    for key in (
        "agent_goal",
        "probe_params",
        "decision_plan",
        "draft_response",
        "execution_status_claim",
        "execution_tier",
    ):
        assist.pop(key, None)
    reply_strategy = (
        dict(assist.get("reply_strategy") or {})
        if isinstance(assist.get("reply_strategy"), dict)
        else {}
    )
    reply_strategy.pop("draft_response", None)
    capability = "native_answer" if route == Route.DIRECT_ANSWER else "ask_user"
    receiver = "core" if route == Route.DIRECT_ANSWER else "user"
    safe_request = {
        "capability": capability,
        "target_route": route.value,
        "receiver_type": receiver,
        "executor": "",
        "input": {},
        "reason": reason,
    }
    response_authority = {
        "schema_version": RESPONSE_AUTHORITY_SCHEMA_VERSION,
        "final_route": route.value,
        "capability_execution_status": "not_started",
        "claim_scope": "none",
        "execution_receipt_refs": [],
        "observation_receipt_refs": [],
        "state_change_receipt_refs": [],
        "external_delivery_status": "not_scheduled",
        "delivery_receipt_refs": [],
        "may_claim_dispatch": False,
        "may_claim_capability_progress": False,
        "may_claim_terminal_report": False,
        "may_claim_completion": False,
        "may_promise_later_delivery": False,
        "renderer": "server_no_execution",
        "reason": reason,
    }
    assist.update(
        {
            "recommended_route": route.value,
            "freshness_required": False,
            "needs_probe": False,
            "needs_agent": False,
            "required_capabilities": (
                [capability] if route == Route.DIRECT_ANSWER else []
            ),
            "reasoning_mode": "direct",
            "execution_tier": "L0_direct_answer",
            "answer_source": "current_context",
            "capability_request": safe_request,
            "reply_strategy": reply_strategy,
            "route_decision": {
                "route": route.value,
                "why_this_route": reason,
            },
            "denied_capability_candidate": proposal,
        }
    )
    projected = replace(
        decision,
        route=route,
        reason=reason,
        requires_confirmation=False,
        selected_probe=None,
        target_agent=None,
        capability=capability,
        freshness_required=False,
        needs_probe=False,
        needs_agent=False,
        needs_user_confirmation=False,
        memory_policy="forget" if route == Route.ASK_USER else decision.memory_policy,
        reasoning_mode="direct",
        required_capabilities=(
            [capability] if route == Route.DIRECT_ANSWER else []
        ),
        capability_request=safe_request,
        signals=list(dict.fromkeys([*(decision.signals or []), signal])),
        constraints=list(
            dict.fromkeys(
                [
                    *(decision.constraints or []),
                    "do not claim capability progress without an execution receipt",
                    "do not promise later delivery without a durable delivery record",
                ]
            )
        ),
        model_assist=assist,
        response_authority=response_authority,
    )
    final_assist = dict(projected.model_assist)
    final_assist["decision_plan"] = DecisionPlan.from_decision("", projected).to_dict()
    projected.model_assist = final_assist
    return projected


def project_governance_route(
    decision: Decision,
    *,
    route: Route,
    reason: str,
    original: Decision | None = None,
    signal: str = "policy:governance_route_projected",
) -> Decision:
    """Project a denied execution candidate onto a canonical governance route.

    BLOCK, HUMAN_REVIEW, and ROLLBACK are terminal governance decisions, not
    alternate Agent executors.  Any Agent/Probe/Skill target, executable input,
    or model-authored progress draft from the candidate must therefore be
    removed before capability availability checks and public rendering.
    """

    if route not in _GOVERNANCE_ROUTES:
        raise ValueError(f"governance route required, got {route.value}")
    capability = {
        Route.BLOCK: "guardian",
        Route.HUMAN_REVIEW: "human_review",
        Route.ROLLBACK: "rollback_audit",
    }[route]
    receiver = "core" if route == Route.ROLLBACK else "guardian"
    candidate = original or decision
    assist = dict(decision.model_assist or {})
    proposal = _proposal_summary(candidate)
    for key in (
        "agent_goal",
        "probe_params",
        "decision_plan",
        "draft_response",
        "execution_status_claim",
    ):
        assist.pop(key, None)
    reply_strategy = (
        dict(assist.get("reply_strategy") or {})
        if isinstance(assist.get("reply_strategy"), dict)
        else {}
    )
    reply_strategy.pop("draft_response", None)
    request = {
        "capability": capability,
        "target_route": route.value,
        "receiver_type": receiver,
        "executor": "",
        "input": {},
        "reason": reason,
    }
    assist.update(
        {
            "recommended_route": route.value,
            "freshness_required": False,
            "needs_probe": False,
            "needs_agent": False,
            "required_capabilities": [capability],
            "reasoning_mode": "execution",
            "execution_tier": "L4_guarded_action",
            "answer_source": "governance_projection",
            "capability_request": request,
            "reply_strategy": reply_strategy,
            "route_decision": {
                "route": route.value,
                "why_this_route": reason,
            },
            "denied_capability_candidate": proposal,
        }
    )
    projected = replace(
        decision,
        route=route,
        reason=reason,
        # HUMAN_REVIEW is itself the confirmation boundary.  ROLLBACK must
        # reach AwarenessLoop._run_rollback_request first so that the
        # RollbackManager can build the concrete snapshot proposal and return
        # its own governed ``needs_confirmation`` result.  Marking it here
        # makes Guardian rewrite the public route to HUMAN_REVIEW too early.
        requires_confirmation=route == Route.HUMAN_REVIEW,
        selected_probe=None,
        target_agent=None,
        capability=capability,
        freshness_required=False,
        needs_probe=False,
        needs_agent=False,
        needs_user_confirmation=route == Route.HUMAN_REVIEW,
        reasoning_mode="execution",
        required_capabilities=[capability],
        capability_request=request,
        signals=list(dict.fromkeys([*(decision.signals or []), signal])),
        constraints=list(
            dict.fromkeys(
                [
                    *(decision.constraints or []),
                    "do not claim execution before the governance boundary authorizes it",
                    "do not reuse the denied candidate executor or arguments",
                ]
            )
        ),
        model_assist=assist,
        response_authority={},
    )
    final_assist = dict(projected.model_assist)
    final_assist["decision_plan"] = DecisionPlan.from_decision(
        "",
        projected,
    ).to_dict()
    return replace(projected, model_assist=final_assist)


def _proposal_summary(decision: Decision) -> dict[str, str]:
    assist = decision.model_assist if isinstance(decision.model_assist, dict) else {}
    requests = [
        _mapping(decision.capability_request),
        _mapping(assist.get("capability_request")),
    ]
    request = next(
        (item for item in requests if _request_is_effectful(item)),
        next((item for item in requests if item), {}),
    )
    route_decision = (
        assist.get("route_decision")
        if isinstance(assist.get("route_decision"), dict)
        else {}
    )
    plan = assist.get("decision_plan") if isinstance(assist.get("decision_plan"), dict) else {}
    audit_hints = plan.get("audit_hints") if isinstance(plan.get("audit_hints"), dict) else {}
    route = str(
        route_decision.get("route")
        or assist.get("recommended_route")
        or request.get("target_route")
        or audit_hints.get("route")
        or decision.route.value
    ).strip()
    capability = str(
        request.get("capability_id")
        or request.get("capability")
        or audit_hints.get("capability")
        or decision.capability
        or ""
    ).strip()
    executor = str(
        decision.selected_probe
        or request.get("executor")
        or plan.get("selected_agent")
        or ""
    ).strip()
    return {
        "route": route[:64],
        "capability": capability[:128],
        "executor": executor[:128],
    }
