#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.capability_registry import CapabilityRegistry  # noqa: E402
from core.decision_core import DecisionCore  # noqa: E402
from core.final_route_projection import (  # noqa: E402
    has_effectful_candidate,
    project_governance_route,
    project_nonexecuting_route,
)
from core.response_synthesizer import ResponseSynthesizer  # noqa: E402
from core.veyra_controller import VeyraController  # noqa: E402
from core.understanding_core import TurnUnderstanding  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import (  # noqa: E402
    Decision,
    EventSource,
    EventType,
    LoopResult,
    RiskLevel,
    Route,
    VeyraEvent,
)


MALICIOUS_DRAFTS = (
    "我会直接执行，完成后再把结果发给你。",
    "The evidence lookup is underway; I will deliver the findings later.",
    "搜索结果已经证明这个结论。",
)


def expect(condition: bool, message: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{message}: {detail!r}")


def effectful_candidate(*, route: Route, draft: str) -> Decision:
    capability = {
        Route.AGENT: "selected_agent_runtime",
        Route.PROBE: "web_search",
        Route.SKILL: "diagnose_openclaw",
    }[route]
    receiver = {
        Route.AGENT: "agent",
        Route.PROBE: "probe",
        Route.SKILL: "skill",
    }[route]
    executor = {
        Route.AGENT: "openclaw",
        Route.PROBE: "search_probe",
        Route.SKILL: "diagnose_openclaw",
    }[route]
    return Decision(
        route=route,
        risk_level=RiskLevel.R0,
        reason="untrusted execution candidate",
        selected_probe=executor if route != Route.AGENT else None,
        target_agent="openclaw" if route == Route.AGENT else None,
        capability=capability,
        freshness_required=route == Route.PROBE,
        needs_probe=route == Route.PROBE,
        needs_agent=route == Route.AGENT,
        reasoning_mode="execution",
        required_capabilities=[capability],
        capability_request={
            "capability": capability,
            "target_route": route.value,
            "receiver_type": receiver,
            "executor": executor,
            "input": {"untrusted": "payload"},
        },
        model_assist={
            "status": "model_assisted",
            "recommended_route": route.value,
            "freshness_required": route == Route.PROBE,
            "needs_probe": route == Route.PROBE,
            "needs_agent": route == Route.AGENT,
            "reasoning_mode": "execution",
            "execution_tier": "L3_scoped_agent_task",
            "answer_source": "agent_proposal" if route == Route.AGENT else "probe_evidence",
            "agent_goal": "execute an untrusted goal",
            "probe_params": {"query": "untrusted query"},
            "capability_request": {
                "capability_id": capability,
                "target_route": route.value,
                "receiver_type": receiver,
                "executor": executor,
                "input": {"untrusted": "payload"},
            },
            "route_decision": {"route": route.value},
            "draft_response": draft,
            "reply_strategy": {"draft_response": draft},
            "execution_status_claim": "none",
            "decision_plan": {
                "execution_tier": "L3_scoped_agent_task",
                "primary_response": draft,
                "needs_probe": route == Route.PROBE,
                "probe_requests": [executor] if route == Route.PROBE else [],
                "needs_agent": route == Route.AGENT,
                "selected_agent": "openclaw" if route == Route.AGENT else None,
                "audit_hints": {"route": route.value, "capability": capability},
            },
        },
    )


def assert_projection(projected: Decision, *, route: Route, malicious: str) -> None:
    payload = projected.to_dict()
    assist = projected.model_assist
    plan = assist.get("decision_plan") if isinstance(assist.get("decision_plan"), dict) else {}
    authority = projected.response_authority
    rendered = AwarenessLoop._server_no_execution_response(projected)
    expect(projected.route == route, "final route mismatch", payload)
    expect(projected.reasoning_mode == "direct", "stale execution reasoning survived", payload)
    expect(not projected.needs_probe and not projected.needs_agent, "execution need survived", payload)
    expect(projected.selected_probe is None and projected.target_agent is None, "executor survived", payload)
    expect(
        projected.capability_request.get("executor") == ""
        and projected.capability_request.get("input") == {},
        "untrusted capability envelope survived",
        payload,
    )
    expect(
        not assist.get("draft_response")
        and not assist.get("reply_strategy", {}).get("draft_response")
        and not assist.get("agent_goal")
        and not assist.get("probe_params"),
        "execution-shaped model fields survived",
        assist,
    )
    expect(
        plan.get("execution_tier") == "L0_direct_answer"
        and plan.get("needs_probe") is False
        and plan.get("needs_agent") is False
        and plan.get("audit_hints", {}).get("route") == route.value,
        "DecisionPlan was not rebuilt from final route",
        plan,
    )
    expect(
        authority.get("capability_execution_status") == "not_started"
        and authority.get("claim_scope") == "none"
        and authority.get("execution_receipt_refs") == []
        and authority.get("may_claim_dispatch") is False
        and authority.get("may_claim_capability_progress") is False
        and authority.get("may_claim_terminal_report") is False
        and authority.get("may_claim_completion") is False
        and authority.get("may_promise_later_delivery") is False
        and authority.get("renderer") == "server_no_execution",
        "response authority was not server-locked",
        authority,
    )
    expect(malicious not in rendered, "malicious draft reached renderer", rendered)
    expect("没有执行" in rendered and "没有安排稍后自动交付" in rendered, "renderer hid no-execution truth", rendered)


def main() -> int:
    for index, route in enumerate((Route.PROBE, Route.AGENT, Route.SKILL)):
        candidate = effectful_candidate(route=route, draft=MALICIOUS_DRAFTS[index])
        expect(has_effectful_candidate(candidate), "structured candidate was not detected", candidate.to_dict())
        projected = project_nonexecuting_route(
            candidate,
            route=Route.DIRECT_ANSWER,
            reason="semantic policy denied execution",
            original=candidate,
        )
        assert_projection(projected, route=Route.DIRECT_ANSWER, malicious=MALICIOUS_DRAFTS[index])

    nested = effectful_candidate(route=Route.AGENT, draft=MALICIOUS_DRAFTS[0])
    nested.route = Route.DIRECT_ANSWER
    nested.capability = "native_answer"
    nested.needs_agent = False
    nested.target_agent = None
    expect(
        has_effectful_candidate(nested),
        "nested Agent provenance was lost after an upstream route rewrite",
        nested.to_dict(),
    )

    shadowed = Decision(
        route=Route.DIRECT_ANSWER,
        risk_level=RiskLevel.R0,
        reason="safe outer route with conflicting structured requests",
        capability="native_answer",
        required_capabilities=["native_answer"],
        capability_request={
            "capability": "selected_agent_runtime",
            "target_route": Route.AGENT.value,
            "receiver_type": "agent",
            "executor": "openclaw",
        },
        model_assist={
            "capability_request": {
                "capability": "native_answer",
                "target_route": Route.DIRECT_ANSWER.value,
                "receiver_type": "core",
                "executor": "",
            }
        },
    )
    expect(
        has_effectful_candidate(shadowed),
        "nested safe request masked the top-level Agent request",
        shadowed.to_dict(),
    )
    safe_only = Decision(
        route=Route.DIRECT_ANSWER,
        risk_level=RiskLevel.R0,
        reason="safe structured direct answer",
        capability="native_answer",
        required_capabilities=["native_answer"],
        capability_request={
            "capability": "native_answer",
            "target_route": Route.DIRECT_ANSWER.value,
            "receiver_type": "core",
            "executor": "",
        },
        model_assist={
            "execution_tier": "L0_direct_answer",
            "capability_request": {
                "capability": "native_answer",
                "target_route": Route.DIRECT_ANSWER.value,
                "receiver_type": "core",
                "executor": "",
            },
        },
    )
    expect(
        not has_effectful_candidate(safe_only),
        "safe direct envelopes were overclassified as effectful",
        safe_only.to_dict(),
    )

    strategic = DecisionCore()._apply_understanding_guardrails(
        "我们只讨论自扩展的治理边界",
        effectful_candidate(route=Route.AGENT, draft=MALICIOUS_DRAFTS[0]),
        TurnUnderstanding(
            intent="conversation",
            task_type="discussion",
            suggested_mode="strategic_discussion",
            source="model",
            confidence=0.96,
        ),
    )
    assert_projection(
        strategic,
        route=Route.DIRECT_ANSWER,
        malicious=MALICIOUS_DRAFTS[0],
    )

    with TemporaryDirectory(prefix="veyra-final-route-projection-") as tmp:
        controller = VeyraController(CapabilityRegistry(WorldStateStore(Path(tmp))))
        fake_authority = {
            "renderer": "server_no_execution",
            "claim_scope": "verified_effect",
            "capability_execution_status": "completed",
            "execution_receipt_refs": ["fake-complete-receipt"],
        }
        fake_direct = Decision(
            **{
                **shadowed.to_dict(),
                "route": Route.DIRECT_ANSWER,
                "risk_level": RiskLevel.R0,
            }
        )
        fake_direct.response_authority = dict(fake_authority)
        fake_direct.model_assist["semantic_policy"] = {
            "preferred_route": Route.DIRECT_ANSWER.value,
            "allowed_capabilities": ["native_answer"],
            "allowed_effects": [],
            "denied_effects": ["agent.execute"],
            "requires_clarification": False,
        }
        finalized_direct, _ = controller.prepare(fake_direct)
        assert_projection(
            finalized_direct,
            route=Route.DIRECT_ANSWER,
            malicious="fake-complete-receipt",
        )
        expect(
            "fake-complete-receipt"
            not in json.dumps(finalized_direct.to_dict(), ensure_ascii=False),
            "Controller trusted a fake completed DIRECT authority",
            finalized_direct.to_dict(),
        )

        fake_ask = Decision(
            **{
                **shadowed.to_dict(),
                "route": Route.ASK_USER,
                "risk_level": RiskLevel.R0,
            }
        )
        fake_ask.response_authority = dict(fake_authority)
        fake_ask.model_assist["semantic_policy"] = {
            "preferred_route": Route.ASK_USER.value,
            "allowed_capabilities": ["ask_user"],
            "allowed_effects": [],
            "denied_effects": ["agent.execute"],
            "requires_clarification": False,
        }
        finalized_ask, _ = controller.prepare(fake_ask)
        assert_projection(
            finalized_ask,
            route=Route.ASK_USER,
            malicious="fake-complete-receipt",
        )
        expect(
            "fake-complete-receipt"
            not in json.dumps(finalized_ask.to_dict(), ensure_ascii=False),
            "Controller trusted a fake completed ASK authority",
            finalized_ask.to_dict(),
        )

        missing_policy = effectful_candidate(route=Route.AGENT, draft=MALICIOUS_DRAFTS[1])
        guarded, _ = controller.prepare(missing_policy)
        assert_projection(guarded, route=Route.ASK_USER, malicious=MALICIOUS_DRAFTS[1])

        unsupported = effectful_candidate(route=Route.PROBE, draft=MALICIOUS_DRAFTS[2])
        unsupported.selected_probe = "novel_probe"
        unsupported.capability_request["executor"] = "novel_probe"
        unsupported.model_assist["capability_request"]["executor"] = "novel_probe"
        unsupported.model_assist["semantic_policy"] = {
            "preferred_route": Route.PROBE.value,
            "selected_probe": "novel_probe",
            "allowed_capabilities": ["novel_probe"],
            "allowed_effects": [],
            "denied_effects": [],
            "requires_clarification": False,
        }
        guarded_unsupported, _ = controller.prepare(unsupported)
        assert_projection(
            guarded_unsupported,
            route=Route.ASK_USER,
            malicious=MALICIOUS_DRAFTS[2],
        )

        for risk, expected_route, expected_capability in (
            (RiskLevel.R3, Route.HUMAN_REVIEW, "human_review"),
            (RiskLevel.R4, Route.HUMAN_REVIEW, "human_review"),
            (RiskLevel.R5, Route.BLOCK, "guardian"),
        ):
            governance_candidate = effectful_candidate(
                route=Route.AGENT,
                draft=MALICIOUS_DRAFTS[0],
            )
            governance_candidate.risk_level = risk
            governance_candidate.model_assist["semantic_policy"] = {
                "preferred_route": Route.ASK_USER.value,
                "allowed_capabilities": ["ask_user"],
                "allowed_effects": [],
                "denied_effects": ["agent.execute"],
                "requires_clarification": False,
            }
            governed, _ = controller.prepare(governance_candidate)
            governed_plan = governed.model_assist.get("decision_plan", {})
            expect(
                governed.route == expected_route,
                f"{risk.value} governance route was weakened",
                governed.to_dict(),
            )
            expect(
                governed.capability == expected_capability
                and governed.required_capabilities == [expected_capability]
                and governed.target_agent is None
                and governed.selected_probe is None
                and governed.needs_agent is False
                and governed.needs_probe is False,
                f"{risk.value} governance route retained the Agent candidate",
                governed.to_dict(),
            )
            expect(
                governed.capability_request.get("target_route")
                == expected_route.value
                and governed.capability_request.get("executor") == ""
                and governed.capability_request.get("input") == {},
                f"{risk.value} governance capability envelope was not rebuilt",
                governed.to_dict(),
            )
            expect(
                not governed.model_assist.get("draft_response")
                and not governed.model_assist.get("agent_goal")
                and governed_plan.get("selected_agent") is None
                and not governed_plan.get("primary_response")
                and governed_plan.get("execution_tier")
                == "L4_guarded_action",
                f"{risk.value} governance DecisionPlan retained execution prose",
                governed_plan,
            )

        degraded_r4 = effectful_candidate(
            route=Route.AGENT,
            draft=MALICIOUS_DRAFTS[0],
        )
        degraded_r4.risk_level = RiskLevel.R4
        degraded_r4.model_assist["semantic_policy"] = {
            "preferred_route": Route.ASK_USER.value,
            "allowed_capabilities": ["ask_user"],
            "allowed_effects": [],
            "denied_effects": ["agent.execute"],
            "requires_clarification": True,
        }
        clarified_r4, _ = controller.prepare(degraded_r4)
        expect(
            clarified_r4.route == Route.ASK_USER
            and clarified_r4.risk_level == RiskLevel.R4
            and clarified_r4.response_authority.get("claim_scope") == "none",
            "degraded R4 semantics created review authority before clarification",
            clarified_r4.to_dict(),
        )

        rollback_candidate = effectful_candidate(
            route=Route.AGENT,
            draft=MALICIOUS_DRAFTS[1],
        )
        rollback_candidate.route = Route.ROLLBACK
        rollback_candidate.risk_level = RiskLevel.R1
        canonical_rollback = project_governance_route(
            rollback_candidate,
            route=Route.ROLLBACK,
            reason="rollback requires the governance boundary",
            original=rollback_candidate,
        )
        finalized_rollback, _ = controller.prepare(canonical_rollback)
        expect(
            finalized_rollback.route == Route.ROLLBACK
            and finalized_rollback.capability == "rollback_audit"
            and finalized_rollback.required_capabilities
            == ["rollback_audit"]
            and finalized_rollback.target_agent is None,
            "rollback governance projection retained an Agent candidate",
            finalized_rollback.to_dict(),
        )

        agent_to_probe = effectful_candidate(
            route=Route.AGENT,
            draft=MALICIOUS_DRAFTS[0],
        )
        agent_to_probe.model_assist["semantic_policy"] = {
            "preferred_route": Route.PROBE.value,
            "selected_probe": "time",
            "allowed_capabilities": ["time_probe"],
            "allowed_effects": [],
            "denied_effects": ["agent.execute"],
            "requires_clarification": False,
        }
        finalized_agent_to_probe, _ = controller.prepare(agent_to_probe)
        agent_to_probe_plan = finalized_agent_to_probe.model_assist.get(
            "decision_plan",
            {},
        )
        expect(
            finalized_agent_to_probe.route == Route.PROBE
            and finalized_agent_to_probe.selected_probe == "time"
            and finalized_agent_to_probe.target_agent is None
            and finalized_agent_to_probe.needs_agent is False,
            "Agent-to-Probe rewrite retained the Agent executor",
            finalized_agent_to_probe.to_dict(),
        )
        expect(
            agent_to_probe_plan.get("selected_agent") is None
            and not agent_to_probe_plan.get("primary_response")
            and MALICIOUS_DRAFTS[0]
            not in json.dumps(
                finalized_agent_to_probe.to_dict(),
                ensure_ascii=False,
            ),
            "Agent-to-Probe rewrite retained Agent progress prose",
            finalized_agent_to_probe.to_dict(),
        )

        for index, candidate_route in enumerate(
            (Route.AGENT, Route.PROBE, Route.SKILL)
        ):
            semantic_ask = effectful_candidate(
                route=candidate_route,
                draft=MALICIOUS_DRAFTS[index],
            )
            semantic_ask.model_assist["semantic_policy"] = {
                "preferred_route": Route.ASK_USER.value,
                "allowed_capabilities": ["ask_user"],
                "allowed_effects": [],
                "denied_effects": [f"{candidate_route.value}.execute"],
                "requires_clarification": False,
            }
            guarded_ask, _ = controller.prepare(semantic_ask)
            assert_projection(
                guarded_ask,
                route=Route.ASK_USER,
                malicious=MALICIOUS_DRAFTS[index],
            )

        stale_probe = effectful_candidate(
            route=Route.PROBE,
            draft=MALICIOUS_DRAFTS[2],
        )
        stale_probe.selected_probe = "time"
        stale_probe.capability = "time_probe"
        stale_probe.required_capabilities = ["time_probe"]
        stale_probe.capability_request.update(
            {
                "capability": "time_probe",
                "executor": "time",
                "probe": "time",
            }
        )
        stale_probe.model_assist["capability_request"] = dict(
            stale_probe.capability_request
        )
        stale_probe.model_assist["semantic_policy"] = {
            "preferred_route": Route.PROBE.value,
            "selected_probe": "time",
            "allowed_capabilities": ["time_probe"],
            "allowed_effects": [],
            "denied_effects": [],
            "requires_clarification": False,
        }
        stale_probe.signals.extend(
            [
                "policy:required_probe_preserved",
                "policy:semantic_cannot_weaken_required_probe",
            ]
        )
        stale_probe.response_authority = {
            "renderer": "server_no_execution",
            "claim_scope": "none",
            "execution_receipt_refs": ["fake-receipt"],
        }
        finalized_probe, _ = controller.prepare(stale_probe)
        probe_plan = finalized_probe.model_assist.get("decision_plan", {})
        expect(finalized_probe.route == Route.PROBE, "authorized Probe route was weakened", finalized_probe.to_dict())
        expect(finalized_probe.response_authority == {}, "stale no-execution authority survived Probe route", finalized_probe.to_dict())
        expect(probe_plan.get("execution_tier") == "L2_probe_verify", "Probe DecisionPlan kept stale tier", probe_plan)
        expect(finalized_probe.capability_request.get("target_route") == Route.PROBE.value, "Probe envelope mismatched final route", finalized_probe.to_dict())

        runtime_only = object.__new__(AwarenessLoop)
        runtime_only.probes = {}
        runtime_event = VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(
                channel="test",
                user_id="projection-user",
                session_id="projection-session",
            ),
            payload={"text": "检查新的观察源"},
        )
        runtime_probe = effectful_candidate(
            route=Route.PROBE,
            draft=MALICIOUS_DRAFTS[2],
        )
        runtime_probe.selected_probe = "unregistered_probe"
        runtime_result = runtime_only._run_single_probe(
            runtime_event,
            runtime_probe,
            [],
        )
        runtime_decision = runtime_result.artifacts.get("decision", {})
        expect(runtime_result.route == Route.ASK_USER, "missing runtime probe did not fail closed", runtime_result.to_dict())
        expect(runtime_decision.get("route") == Route.ASK_USER.value, "runtime artifact retained Probe route", runtime_decision)
        expect(runtime_decision.get("response_authority", {}).get("claim_scope") == "none", "runtime ASK lacks no-execution authority", runtime_decision)

        shortcut_result = LoopResult(
            event_id="evt-shortcut",
            route=Route.DIRECT_ANSWER,
            status="success",
            response=MALICIOUS_DRAFTS[0],
            risk_level=RiskLevel.R0,
            artifacts={
                "decision": effectful_candidate(
                    route=Route.AGENT,
                    draft=MALICIOUS_DRAFTS[0],
                ).to_dict()
            },
        )
        runtime_only._finalize_result_response_authority(shortcut_result)
        shortcut_authority = shortcut_result.artifacts.get("response_authority", {})
        expect(shortcut_authority.get("claim_scope") == "none", "shortcut result invented execution authority", shortcut_result.to_dict())
        expect(
            shortcut_result.artifacts.get("decision", {}).get("response_authority")
            == shortcut_authority,
            "terminal and Decision response-authority contracts diverged",
            shortcut_result.to_dict(),
        )
        expect(MALICIOUS_DRAFTS[0] not in shortcut_result.response, "shortcut bypassed terminal renderer", shortcut_result.response)
        expect(
            MALICIOUS_DRAFTS[0]
            not in str(shortcut_result.primary_response or ""),
            "shortcut primary response bypassed terminal renderer",
            shortcut_result.primary_response,
        )

        projected_ask = project_nonexecuting_route(
            effectful_candidate(
                route=Route.AGENT,
                draft=MALICIOUS_DRAFTS[0],
            ),
            route=Route.ASK_USER,
            reason="the referent is ambiguous",
        )
        specific_clarification = (
            "你说的对象目前不唯一，请确认是方案 A 还是方案 B。"
        )
        projected_ask_result = LoopResult(
            event_id="evt-projected-ask",
            route=Route.ASK_USER,
            status="needs_user_input",
            response=specific_clarification,
            risk_level=RiskLevel.R1,
            artifacts={"decision": projected_ask.to_dict()},
        )
        runtime_only._finalize_result_response_authority(
            projected_ask_result
        )
        expect(
            projected_ask_result.response == specific_clarification,
            "terminal finalizer replaced a server-owned specific clarification",
            projected_ask_result.to_dict(),
        )

        projected_direct = project_nonexecuting_route(
            effectful_candidate(
                route=Route.AGENT,
                draft=MALICIOUS_DRAFTS[1],
            ),
            route=Route.DIRECT_ANSWER,
            reason="strategic discussion stays in Veyra Core",
        )
        strategic_response = (
            "这是一项治理边界讨论；当前应先比较方案，不执行候选。"
        )
        projected_direct_result = LoopResult(
            event_id="evt-projected-direct",
            route=Route.DIRECT_ANSWER,
            status="success",
            response=strategic_response,
            risk_level=RiskLevel.R0,
            artifacts={"decision": projected_direct.to_dict()},
        )
        runtime_only._finalize_result_response_authority(
            projected_direct_result
        )
        expect(
            projected_direct_result.response == strategic_response,
            "terminal finalizer replaced a server-owned strategic answer",
            projected_direct_result.to_dict(),
        )

        observed_probe_result = LoopResult(
            event_id="evt-probe-observed",
            route=Route.PROBE,
            status="verified_success",
            response="observed result",
            risk_level=RiskLevel.R1,
            artifacts={
                "execution_trace": {"trace_id": "exec_observed_probe"},
                "verification": {"status": "verified_success"},
            },
        )
        runtime_only._finalize_result_response_authority(observed_probe_result)
        observed_authority = observed_probe_result.artifacts.get("response_authority", {})
        expect(observed_authority.get("claim_scope") == "terminal_report", "verified Probe observation lacked terminal authority", observed_authority)
        expect(observed_authority.get("may_claim_completion") is False, "Probe observation was promoted to a persistent effect", observed_authority)

        submitted_response = ResponseSynthesizer().agent_response(
            {
                "status": "submitted",
                "executor": "openclaw",
                "summary": "",
            },
            {
                "status": "partially_success",
                "verdict": "agent_result_pending",
            },
        )
        expect(
            "不承诺稍后自动交付" in submitted_response
            and "我会等待" not in submitted_response,
            "submitted Agent renderer promised an unrecorded future delivery",
            submitted_response,
        )
        submitted_agent_result = LoopResult(
            event_id="evt-agent-submitted",
            route=Route.AGENT,
            status="partially_success",
            response=submitted_response,
            risk_level=RiskLevel.R1,
            artifacts={
                "execution_result": {
                    "task_id": "agent-run-submitted",
                    "executor": "openclaw",
                    "status": "submitted",
                    "result": "awaiting exact terminal observation",
                },
                "pending_task": {
                    "registered": True,
                    "status": "submitted",
                    "verification_status": "partially_success",
                },
                "execution_trace": {"trace_id": "exec_agent_submitted"},
                "verification": {
                    "status": "partially_success",
                    "verdict": "agent_result_pending",
                },
                "profile_state_change": {
                    "status": "committed",
                    "applied": True,
                    "proposal_id": "profile-write-unrelated",
                },
            },
        )
        runtime_only._finalize_result_response_authority(submitted_agent_result)
        submitted_authority = submitted_agent_result.artifacts.get(
            "response_authority",
            {},
        )
        expect(
            submitted_authority.get("claim_scope") == "dispatch_only",
            "submitted Agent result was promoted beyond dispatch authority",
            submitted_authority,
        )
        expect(
            submitted_authority.get("may_claim_terminal_report") is False
            and submitted_authority.get("may_claim_completion") is False,
            "submitted Agent result can claim a terminal outcome",
            submitted_authority,
        )

        malicious_terminal_summary = "已经完成部署并推送到生产。"
        terminal_report_response = ResponseSynthesizer().agent_response(
            {
                "status": "success",
                "executor": "openclaw",
                "summary": malicious_terminal_summary,
            },
            {
                "status": "partially_success",
                "verdict": "terminal_report_without_persistent_effect",
            },
        )
        terminal_agent_result = LoopResult(
            event_id="evt-agent-terminal",
            route=Route.AGENT,
            status="partially_success",
            response=terminal_report_response,
            risk_level=RiskLevel.R1,
            artifacts={
                "execution_result": {
                    "task_id": "agent-run-terminal",
                    "executor": "openclaw",
                    "status": "success",
                    "result": "reported result without verified effect",
                },
                "execution_trace": {"trace_id": "exec_agent_terminal"},
                "verification": {
                    "status": "partially_success",
                    "verdict": "reported_result_not_effect_evidence",
                },
            },
        )
        runtime_only._finalize_result_response_authority(terminal_agent_result)
        terminal_agent_authority = terminal_agent_result.artifacts.get(
            "response_authority",
            {},
        )
        expect(
            terminal_agent_authority.get("claim_scope") == "terminal_report"
            and terminal_agent_authority.get("may_claim_completion") is False,
            "terminal Agent observation lost its report-only boundary",
            terminal_agent_authority,
        )
        expect(
            "Agent 报告" in terminal_agent_result.response
            and "尚未验证为持久执行效果" in terminal_agent_result.response
            and "不能据此标记完成" in terminal_agent_result.response,
            "partial Agent terminal summary escaped without an evidence qualifier",
            terminal_agent_result.response,
        )

        unrelated_committed_result = LoopResult(
            event_id="evt-state-committed",
            route=Route.DIRECT_ANSWER,
            status="success",
            response="state change committed",
            risk_level=RiskLevel.R1,
            artifacts={
                "decision": effectful_candidate(
                    route=Route.AGENT,
                    draft=MALICIOUS_DRAFTS[0],
                ).to_dict(),
                "profile_state_change": {
                    "status": "committed",
                    "applied": True,
                    "proposal_id": "proposal-observed",
                },
            },
        )
        runtime_only._finalize_result_response_authority(
            unrelated_committed_result
        )
        unrelated_authority = unrelated_committed_result.artifacts.get(
            "response_authority",
            {},
        )
        expect(
            unrelated_authority.get("claim_scope") == "none"
            and unrelated_authority.get("may_claim_completion") is False,
            "an unrelated internal write certified the denied Agent candidate",
            unrelated_authority,
        )

        bound_commit_result = LoopResult(
            event_id="evt-bound-state-commit",
            route=Route.DIRECT_ANSWER,
            status="success",
            response="已暂停：Aurora 状态跟踪",
            risk_level=RiskLevel.R1,
            artifacts={
                "decision": effectful_candidate(
                    route=Route.AGENT,
                    draft=MALICIOUS_DRAFTS[0],
                ).to_dict(),
                "commitment": {
                    "status": "paused",
                    "primary_response_override": "已暂停：Aurora 状态跟踪",
                    "state_change": {
                        "status": "committed",
                        "applied": True,
                        "proposal_id": "commitment-change-observed",
                    },
                },
                "commitment_primary_applied": "已暂停：Aurora 状态跟踪",
            },
        )
        runtime_only._finalize_result_response_authority(bound_commit_result)
        bound_commit_authority = bound_commit_result.artifacts.get(
            "response_authority",
            {},
        )
        expect(
            bound_commit_authority.get("claim_scope") == "verified_effect"
            and bound_commit_authority.get("state_change_receipt_refs")
            == ["state_change:commitment-change-observed"]
            and bound_commit_authority.get("may_claim_completion") is True,
            "response-bound committed state change lost its exact effect authority",
            bound_commit_authority,
        )
        expect(
            bound_commit_result.response == "已暂停：Aurora 状态跟踪",
            "an exact committed state response was replaced by no-execution prose",
            bound_commit_result.to_dict(),
        )
        expect(
            bound_commit_authority.get("may_claim_dispatch") is False
            and bound_commit_authority.get("may_claim_capability_progress")
            is False,
            "an internal state receipt invented dispatch or progress authority",
            bound_commit_authority,
        )

    print(
        json.dumps(
            {
                "schema": "veyra.final_route_projection_smoke.v1",
                "status": "ok",
                "checks": [
                    "probe agent skill candidates project atomically",
                    "nested effectful provenance survives upstream route rewrites",
                    "top-level effectful requests cannot be masked by nested safe requests",
                    "safe structured envelopes remain ordinary direct answers",
                    "strategic discussion guardrail preserves denied execution provenance",
                    "missing policy and unsupported probes fail closed",
                    "R3 R4 R5 and rollback routes discard denied Agent provenance",
                    "Agent-to-Probe rewrites discard Agent progress provenance",
                    "Controller finalizer aligns ASK and authorized Probe terminal routes",
                    "runtime Probe-to-ASK transitions project their public Decision",
                    "server-owned clarification and strategic answers survive terminal projection",
                    "unrelated state writes cannot certify another routed capability",
                    "terminal result authority distinguishes none, dispatch, report, and verified effect",
                    "DecisionPlan and Foresight-facing reasoning match final route",
                    "server renderer ignores model execution claims and later-delivery promises",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
