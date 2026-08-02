#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognition_pipeline import CognitionPipeline  # noqa: E402
from core.decision_core import DecisionCore  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.semantic_policy import SemanticPolicyCompiler  # noqa: E402
from core.understanding_core import TurnUnderstanding  # noqa: E402
from interface.event_schema import Decision, Route  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def understanding(
    text: str,
    *,
    kind: str,
    operation: str,
    target_type: str,
    suggested_mode: str,
    condition: dict[str, Any] | None = None,
) -> TurnUnderstanding:
    # Deliberately mark both examples as implementation/code_task.  The exact
    # semantic act must still distinguish discussion-about-implementation from
    # an instruction to implement.  This guards against a lexical route flip.
    condition_payload = dict(condition) if isinstance(condition, dict) else None
    if condition_payload is not None and isinstance(condition_payload.get("source_quote"), str):
        quote_text = str(condition_payload["source_quote"])
        quote_start = text.index(quote_text)
        condition_payload["source_quote"] = {
            "text": quote_text,
            "start": quote_start,
            "end": quote_start + len(quote_text),
        }
    return TurnUnderstanding.from_payload(
        {
            "status": "model_assisted",
            "situation_assessment": {
                "intent": "implementation",
                "task_type": "code_task",
                "explicit_request": text,
                "task_summary": text,
                "suggested_mode": suggested_mode,
                "confidence": 0.98,
                "evidence_gap": {
                    "needs_fresh_evidence": False,
                    "evidence_kind": "none",
                },
            },
            "semantic_frame": {
                "schema_version": "veyra.semantic_frame.v1",
                "acts": [
                    {
                        "act_id": "a1",
                        "kind": kind,
                        "goal": text,
                        "operation": operation,
                        "target": {
                            "type": target_type,
                            "value": "Veyra self-extension runner boundary",
                            "attributes": {},
                        },
                        "polarity": "positive",
                        "explicitness": "explicit",
                        "source_quote": {
                            "text": text,
                            "start": 0,
                            "end": len(text),
                        },
                        "speaker": "user",
                        "authority": "direct_user",
                        "mention_mode": "normal_use",
                        "evidence_need": "none",
                        "referent": {
                            "surface": "Veyra self-extension runner boundary",
                            "resolved": "Veyra self-extension runner boundary",
                            "status": "resolved",
                            "candidates": [],
                        },
                        "condition": condition_payload,
                        "modality": "asserted",
                        "arguments": {},
                    }
                ],
                "relations": [],
                "ambiguities": [],
                "resolver_status": "resolved",
                "source": "model",
            },
        },
        source_text=text,
    )


def agent_candidate() -> Decision:
    return Decision(
        route=Route.AGENT,
        risk_level=RiskLevel.R1,
        reason="candidate implementation route",
        intent="implementation",
        complexity="complex",
        capability="selected_agent_runtime",
        needs_agent=True,
        required_capabilities=["selected_agent_runtime"],
    )


def main() -> int:
    discuss_text = "请分析 Veyra 自扩展 runner 的实现边界是否合理。"
    execute_text = "请实现 Veyra 自扩展 runner。"
    discussion = understanding(
        discuss_text,
        kind="information_request",
        operation="analyze_architecture_boundary",
        target_type="architecture_topic",
        suggested_mode="strategic_discussion",
    )
    execution = understanding(
        execute_text,
        kind="implementation",
        operation="build",
        target_type="component",
        suggested_mode="strategic_discussion",
    )
    constrained_discussion = understanding(
        "在不扩大执行权的前提下，分析下一步最值得补的认知闭环。",
        kind="information_request",
        operation="analyze_cognitive_loop",
        target_type="architecture_topic",
        suggested_mode="strategic_discussion",
        condition={
            "kind": "governance_constraint",
            "expression": "do not expand execution authority",
            "source_quote": "在不扩大执行权的前提下",
        },
    )
    conditional_execution = understanding(
        "如果隔离 runner 可用，请实现候选扩展。",
        kind="implementation",
        operation="build",
        target_type="component",
        suggested_mode="governed_execution",
        condition={
            "kind": "runtime_precondition",
            "expression": "isolated runner is available",
            "source_quote": "如果隔离 runner 可用",
        },
    )

    expect(
        not discussion.requests_governed_effect_or_runtime(),
        "discussion target mentioning implementation grants no execution intent",
    )
    expect(
        execution.requests_governed_effect_or_runtime(),
        "explicit implementation act preserves governed execution intent",
    )
    expect(
        not constrained_discussion.requests_governed_effect_or_runtime(),
        "read-only discussion constraint grants no governed effect",
    )
    expect(
        conditional_execution.requests_governed_effect_or_runtime(),
        "conditional implementation remains a governed effect request",
    )

    compiler = SemanticPolicyCompiler()
    discussion_policy = compiler.compile(constrained_discussion.semantic_frame)
    execution_policy = compiler.compile(conditional_execution.semantic_frame)
    expect(
        discussion_policy.preferred_route == Route.DIRECT_ANSWER.value
        and not discussion_policy.requires_clarification
        and "semantic_policy:read_only_condition_non_authorizing"
        in discussion_policy.policy_signals,
        "read-only condition stays a non-authorizing answer constraint",
        discussion_policy.to_dict(),
    )
    expect(
        execution_policy.preferred_route == Route.ASK_USER.value
        and execution_policy.requires_clarification
        and "semantic_policy:conditional_effect_unresolved"
        in execution_policy.policy_signals,
        "unresolved execution precondition still fails closed",
        execution_policy.to_dict(),
    )

    cognition = CognitionPipeline.__new__(CognitionPipeline)
    expect(
        not cognition._explicitly_asks_execution_or_runtime(
            discuss_text,
            discussion,
        ),
        "cognition pipeline keeps architecture discussion read-only",
    )
    expect(
        cognition._explicitly_asks_execution_or_runtime(
            execute_text,
            execution,
        ),
        "cognition pipeline recognizes the command minimal pair",
    )

    decisions = DecisionCore()
    guarded_discussion = decisions._apply_understanding_guardrails(
        discuss_text,
        agent_candidate(),
        discussion,
    )
    guarded_execution = decisions._apply_understanding_guardrails(
        execute_text,
        agent_candidate(),
        execution,
    )
    guarded_constrained_discussion = decisions._apply_understanding_guardrails(
        constrained_discussion.explicit_request,
        agent_candidate(),
        constrained_discussion,
    )
    governed_conditional_execution = decisions._enforce_semantic_policy(
        agent_candidate(),
        conditional_execution,
    )
    expect(
        guarded_discussion.route == Route.DIRECT_ANSWER
        and not guarded_discussion.needs_agent,
        "decision guard rejects Agent dispatch for discussion",
        guarded_discussion.to_dict(),
    )
    expect(
        guarded_execution.route == Route.AGENT
        and guarded_execution.needs_agent,
        "decision guard does not erase an explicit implementation command",
        guarded_execution.to_dict(),
    )
    expect(
        guarded_constrained_discussion.route == Route.DIRECT_ANSWER
        and not guarded_constrained_discussion.needs_agent,
        "decision guard keeps constrained architecture discussion read-only",
        guarded_constrained_discussion.to_dict(),
    )
    expect(
        governed_conditional_execution.route == Route.ASK_USER
        and not governed_conditional_execution.needs_agent,
        "semantic enforcement blocks conditional execution pending trigger evidence",
        governed_conditional_execution.to_dict(),
    )

    print("Strategic discussion boundary smoke passed: 12/12")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
