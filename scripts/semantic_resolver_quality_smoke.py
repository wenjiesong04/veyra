#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.semantic_frame import SCHEMA_VERSION, TurnSemanticFrame, semantic_frame_quality_issues  # noqa: E402
from core.semantic_policy import SemanticPolicyCompiler  # noqa: E402
from core.understanding_core import UnderstandingCore  # noqa: E402
from core.capability_registry import CapabilityRegistry  # noqa: E402
from core.veyra_controller import VeyraController  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import Decision, RiskLevel, Route  # noqa: E402


TEXT = "不要每天推送天气，只告诉我现在上海天气。"


def quote(text: str, value: str) -> dict[str, Any]:
    start = text.index(value)
    return {"text": value, "start": start, "end": start + len(value)}


def target(kind: str, value: str, **attributes: Any) -> dict[str, Any]:
    return {"type": kind, "value": value, "attributes": attributes}


def act(
    text: str,
    *,
    act_id: str,
    kind: str,
    goal: str,
    operation: str,
    source: str,
    semantic_target: dict[str, Any],
    polarity: str = "positive",
    evidence_need: str = "none",
    mention_mode: str = "normal_use",
    authority: str = "direct_user",
    speaker: str = "user",
    condition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "act_id": act_id,
        "kind": kind,
        "goal": goal,
        "operation": operation,
        "target": semantic_target,
        "polarity": polarity,
        "explicitness": "explicit",
        "source_quote": quote(text, source),
        "speaker": speaker,
        "authority": authority,
        "mention_mode": mention_mode,
        "evidence_need": evidence_need,
        "referent": {
            "surface": "",
            "resolved": "",
            "status": "not_applicable",
            "candidates": [],
        },
        "condition": condition,
        "modality": "conditional" if condition else "asserted",
        "arguments": {},
    }


def frame_payload(acts: list[dict[str, Any]], *, relations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "acts": acts,
        "relations": relations or [],
        "ambiguities": [],
        "resolver_status": "resolved",
        "source": "model",
    }


def collapsed_frame() -> dict[str, Any]:
    return frame_payload(
        [
            act(
                TEXT,
                act_id="a1",
                kind="prohibition",
                goal="do not push weather; only answer current weather",
                operation="stop daily updates and query current weather",
                source=TEXT,
                semantic_target=target("weather", "上海"),
                polarity="negative",
                evidence_need="fresh_external",
            )
        ]
    )


def complete_frame() -> dict[str, Any]:
    left = "不要每天推送天气"
    right = "只告诉我现在上海天气"
    return frame_payload(
        [
            act(
                TEXT,
                act_id="a1",
                kind="prohibition",
                goal="avoid recurring weather push",
                operation="create_recurring_task",
                source=left,
                semantic_target=target("commitment", "weather_push", frequency="daily"),
                polarity="negative",
            ),
            act(
                TEXT,
                act_id="a2",
                kind="request",
                goal="know current Shanghai weather",
                operation="query_current_weather",
                source=right,
                semantic_target=target("weather", "上海"),
                evidence_need="fresh_external",
            ),
        ],
        relations=[
            {
                "relation_id": "r1",
                "kind": "contrast",
                "from_act_id": "a1",
                "to_act_id": "a2",
                "description": "The current query replaces the prohibited recurring push.",
                "source_quote": None,
            }
        ],
    )


def model_result(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "model_assisted",
        "situation_assessment": {
            "intent": "information",
            "task_type": "current_fact",
            "task_summary": "query current weather without recurring push",
            "explicit_request": TEXT,
            "user_goal": "know current Shanghai weather",
            "suggested_mode": "runtime_evidence",
            "evidence_gap": {
                "needs_fresh_evidence": True,
                "evidence_kind": "weather",
                "what_would_change_the_answer": "fresh weather observation",
            },
            "confidence": 0.95,
        },
        "semantic_frame": copy.deepcopy(frame),
    }


class ScriptedClient:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = [copy.deepcopy(item) for item in outputs]
        self.calls: list[str] = []

    def complete_json(self, *, system: str, user: str, purpose: str) -> dict[str, Any]:
        del system, user
        self.calls.append(purpose)
        return copy.deepcopy(self.outputs.pop(0))


class ScriptedReasoning:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.client = ScriptedClient(outputs)
        self.traces: list[dict[str, Any]] = []

    def is_enabled(self) -> bool:
        return True

    def _trace(self, purpose: str, result: dict[str, Any], summary: dict[str, Any]) -> None:
        self.traces.append({"purpose": purpose, "status": result.get("status"), "summary": summary})


def expect(condition: bool, message: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{message}: {detail!r}")


def resolve(outputs: list[dict[str, Any]]) -> tuple[Any, ScriptedReasoning]:
    reasoning = ScriptedReasoning(outputs)
    understanding = UnderstandingCore(reasoning).build(
        text=TEXT,
        attention_focus=[],
        turn_context={},
        awareness_snapshot={},
    )
    return understanding, reasoning


def main() -> int:
    collapsed = TurnSemanticFrame.from_model_payload({"semantic_frame": collapsed_frame()}, source_text=TEXT)
    collapsed_issues = semantic_frame_quality_issues(collapsed, TEXT)
    expect(
        any("missing_local_act" in issue or "collapsed_hard_boundary" in issue for issue in collapsed_issues),
        "collapsed compound frame was not rejected",
        collapsed_issues,
    )

    complete = TurnSemanticFrame.from_model_payload({"semantic_frame": complete_frame()}, source_text=TEXT)
    expect(not semantic_frame_quality_issues(complete, TEXT), "complete compound frame failed quality gate")

    repaired, repair_reasoning = resolve([model_result(collapsed_frame()), model_result(complete_frame())])
    repaired_policy = SemanticPolicyCompiler().compile(repaired.semantic_frame)
    expect(
        repair_reasoning.client.calls == ["turn_understanding", "turn_understanding_repair"],
        "semantic repair was not exactly one bounded retry",
        repair_reasoning.client.calls,
    )
    expect(repaired.semantic_frame.resolver_status == "resolved", "repaired frame was not accepted")
    expect(repaired_policy.preferred_route == "probe", "repaired compound request did not authorize read-only probe")
    expect(repaired_policy.selected_probe == "weather_probe", "weather probe selection was lost")
    expect("proactive.create" in repaired_policy.denied_effects, "recurring side effect was not denied")

    locked, locked_reasoning = resolve([model_result(collapsed_frame()), model_result(collapsed_frame())])
    locked_policy = SemanticPolicyCompiler().compile(locked.semantic_frame)
    expect(len(locked_reasoning.client.calls) == 2, "invalid frame retried more or less than once")
    expect(locked.semantic_frame.resolver_status == "invalid_output", "double-invalid frame did not lock")
    expect(locked_policy.preferred_route == "ask_user", "invalid output did not fail closed")
    expect(locked_policy.selected_probe is None, "invalid output retained a probe authorization")
    expect(not locked_policy.allowed_effects, "invalid output retained durable effects")
    invalid_explanation_text = "为什么效果不好？请先解释，不要改代码。"
    invalid_explanation_policy = SemanticPolicyCompiler().compile(
        TurnSemanticFrame.fallback(
            invalid_explanation_text,
            resolver_status="invalid_output",
        )
    )
    expect(
        invalid_explanation_policy.preferred_route == "direct_answer"
        and not invalid_explanation_policy.allowed_effects
        and invalid_explanation_policy.allowed_capabilities == ["native_answer"],
        "invalid structured output blocked a side-effect-free explanation instead of degrading read-only",
        invalid_explanation_policy.to_dict(),
    )
    for invalid_execution_text in (
        "把附件做成一种我没定义过的新格式",
        "Turn the attachment into a frobnicated archive.",
        "你能把附件做成星云折叠格式吗？",
    ):
        invalid_execution_policy = SemanticPolicyCompiler().compile(
            TurnSemanticFrame.fallback(
                invalid_execution_text,
                resolver_status="invalid_output",
            )
        )
        expect(
            invalid_execution_policy.preferred_route == "ask_user"
            and not invalid_execution_policy.allowed_effects
            and invalid_execution_policy.selected_probe is None,
            "invalid structured output treated an unknown execution request as read-only",
            {
                "text": invalid_execution_text,
                "policy": invalid_execution_policy.to_dict(),
            },
        )
    empty_invalid_policy = SemanticPolicyCompiler().compile(
        {
            "schema_version": "veyra.semantic_frame.v1",
            "acts": [],
            "relations": [],
            "ambiguities": [],
            "resolver_status": "invalid_output",
            "source": "fallback",
        }
    )
    expect(
        empty_invalid_policy.preferred_route == "ask_user"
        and not empty_invalid_policy.allowed_effects,
        "empty invalid semantic output did not fail closed",
        empty_invalid_policy.to_dict(),
    )

    transport, transport_reasoning = resolve(
        [{"status": "error", "purpose": "turn_understanding", "error": "simulated timeout"}]
    )
    transport_policy = SemanticPolicyCompiler().compile(transport.semantic_frame)
    expect(len(transport_reasoning.client.calls) == 1, "transport error should not trigger semantic repair")
    expect(transport.semantic_frame.resolver_status == "degraded", "transport failure was not distinguished from invalid output")
    expect(not transport_policy.allowed_effects, "transport degradation authorized a durable effect")

    for size in (801, 10_000):
        long_text = "x" * size
        for long_frame in (
            TurnSemanticFrame.fallback(long_text),
            TurnSemanticFrame.safe_from_model_payload({}, source_text=long_text),
        ):
            long_policy = SemanticPolicyCompiler().compile(long_frame)
            source = long_frame.acts[0].source_quote
            expect(
                source.text == long_text[source.start : source.end]
                and len(source.text) <= 800,
                "long fallback input lost its bounded exact source quote",
                long_frame.model_dump(mode="json"),
            )
            expect(
                any(
                    ambiguity.kind == "fallback_input_truncated"
                    for ambiguity in long_frame.ambiguities
                )
                and long_policy.preferred_route == "ask_user"
                and not long_policy.allowed_effects,
                "long fallback input did not fail closed",
                long_policy.to_dict(),
            )

    quoted_text = "解释“不要执行，只解释”是什么意思。"
    quoted = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        quoted_text,
                        act_id="a1",
                        kind="request",
                        goal="explain the quoted phrase",
                        operation="explain_term",
                        source=quoted_text,
                        semantic_target=target("quoted_term", "不要执行，只解释"),
                        mention_mode="quoted_term",
                    )
                ]
            )
        },
        source_text=quoted_text,
    )
    expect(
        not semantic_frame_quality_issues(quoted, quoted_text),
        "discourse markers inside a quotation caused a false structural rejection",
    )

    cessation_text = "PyTorch 那条以后别再跟了。"
    cessation = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        cessation_text,
                        act_id="a1",
                        kind="commitment_control",
                        goal="停止跟进 PyTorch",
                        operation="discontinue_monitoring",
                        source=cessation_text,
                        semantic_target=target("tracked_topic", "PyTorch"),
                    )
                ]
            )
        },
        source_text=cessation_text,
    )
    expect(
        not semantic_frame_quality_issues(cessation, cessation_text),
        "lexicalized cancellation was mistaken for a missing prohibition",
    )

    unsafe_text = "不要删除仓库。"
    unsafe_positive = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        unsafe_text,
                        act_id="a1",
                        kind="workspace_task",
                        goal="删除仓库",
                        operation="delete_repository",
                        source=unsafe_text,
                        semantic_target=target("repository", "current"),
                    )
                ]
            )
        },
        source_text=unsafe_text,
    )
    expect(
        "explicit_negative_surface_missing_negative_act"
        in semantic_frame_quality_issues(unsafe_positive, unsafe_text),
        "negative surface incorrectly authorized a positive destructive operation",
    )

    identified_user_text = "帮我重启 Veyra 服务。"
    identified_user_frame = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        identified_user_text,
                        act_id="a1",
                        kind="workspace_task",
                        goal="重启 Veyra 服务",
                        operation="restart",
                        source=identified_user_text,
                        semantic_target=target("service", "Veyra"),
                        speaker="guard-smoke-user",
                        authority="direct_user",
                    )
                ]
            )
        },
        source_text=identified_user_text,
    )
    identified_user_policy = SemanticPolicyCompiler().compile(
        identified_user_frame,
        current_user_id="guard-smoke-user",
    )
    expect(
        identified_user_policy.preferred_route == "agent"
        and "agent.execute" in identified_user_policy.allowed_effects,
        "a direct-user authority was mistaken for reported speech when speaker contained the user id",
        identified_user_policy.to_dict(),
    )

    actor_alias_text = "Veyra 现在是什么状态？"
    actor_alias_frame = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        actor_alias_text,
                        act_id="a1",
                        kind="information_request",
                        goal="了解 Veyra 当前状态",
                        operation="explain_status",
                        source=actor_alias_text,
                        semantic_target=target("project", "Veyra"),
                        speaker="direct_user",
                        authority="direct_user",
                    )
                ]
            )
        },
        source_text=actor_alias_text,
    )
    actor_alias_policy = SemanticPolicyCompiler().compile(actor_alias_frame)
    expect(
        actor_alias_policy.preferred_route == "direct_answer"
        and not actor_alias_policy.requires_clarification
        and not actor_alias_policy.allowed_effects,
        "provider direct_user actor alias was mistaken for conflicting authority",
        actor_alias_policy.to_dict(),
    )

    read_only_agent_text = "请让 Agent 只读分析 Veyra 目录，不修改文件。"
    read_only_agent_frame = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        read_only_agent_text,
                        act_id="a1",
                        kind="workspace_task",
                        goal="只读分析 Veyra 目录",
                        operation="analyze_repository",
                        source="请让 Agent 只读分析 Veyra 目录",
                        semantic_target=target("repository", "Veyra"),
                    ),
                    act(
                        read_only_agent_text,
                        act_id="a2",
                        kind="prohibition",
                        goal="保持文件不变",
                        operation="modify_code",
                        source="不修改文件",
                        semantic_target=target("repository", "Veyra"),
                        polarity="negative",
                    ),
                ]
            )
        },
        source_text=read_only_agent_text,
    )
    read_only_agent_policy = SemanticPolicyCompiler().compile(read_only_agent_frame)
    expect(
        read_only_agent_policy.preferred_route == "agent"
        and "agent.execute" in read_only_agent_policy.allowed_effects
        and "workspace.write" not in read_only_agent_policy.allowed_effects
        and "workspace.write" in read_only_agent_policy.denied_effects,
        "a no-write constraint incorrectly disabled read-only Agent analysis or retained workspace write",
        read_only_agent_policy.to_dict(),
    )

    denial_text = "不要取消 PyTorch，只解释一下 cancel_task。"
    denial_frame = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        denial_text,
                        act_id="a1",
                        kind="prohibition",
                        goal="保持 PyTorch 跟踪任务",
                        operation="cancel_task",
                        source="不要取消 PyTorch",
                        semantic_target=target("tracked_topic", "PyTorch"),
                        polarity="negative",
                    ),
                    act(
                        denial_text,
                        act_id="a2",
                        kind="request",
                        goal="解释 cancel_task 的含义",
                        operation="explain",
                        source="只解释一下 cancel_task",
                        semantic_target=target("concept", "cancel_task"),
                    ),
                ],
                relations=[
                    {
                        "relation_id": "r1",
                        "kind": "contrast",
                        "from_act_id": "a1",
                        "to_act_id": "a2",
                        "description": "explanation only",
                        "source_quote": None,
                    }
                ],
            )
        },
        source_text=denial_text,
    )
    denial_policy = SemanticPolicyCompiler().compile(denial_frame)
    expect(
        denial_policy.preferred_route == "direct_answer"
        and "commitment.mutate" not in denial_policy.allowed_effects
        and not denial_policy.effect_authorized_act_ids,
        "a prohibited cancellation was re-opened by an explanation act",
        denial_policy.to_dict(),
    )

    conditional_text = "等我确认后再部署。"
    conditional_action = act(
        conditional_text,
        act_id="a1",
        kind="workspace_task",
        goal="确认后部署",
        operation="deploy",
        source="等我确认后再部署",
        semantic_target=target("service", "current"),
        condition={
            "kind": "after_confirmation",
            "expression": "等我确认",
            "source_quote": quote(conditional_text, "等我确认"),
        },
    )
    conditional_frame = TurnSemanticFrame.from_model_payload(
        {"semantic_frame": frame_payload([conditional_action])},
        source_text=conditional_text,
    )
    conditional_policy = SemanticPolicyCompiler().compile(conditional_frame)
    expect(
        conditional_policy.preferred_route == "ask_user"
        and not conditional_policy.allowed_effects,
        "an unresolved conditional action was authorized",
        conditional_policy.to_dict(),
    )

    conditional_read_text = "如果下雨再查上海天气。"
    conditional_read = act(
        conditional_read_text,
        act_id="a1",
        kind="request",
        goal="下雨时查询上海天气",
        operation="query_current_weather",
        source="如果下雨再查上海天气",
        semantic_target=target("weather", "上海"),
        evidence_need="fresh_external_weather",
        condition={
            "kind": "if",
            "expression": "下雨",
            "source_quote": quote(conditional_read_text, "下雨"),
        },
    )
    conditional_read_frame = TurnSemanticFrame.from_model_payload(
        {"semantic_frame": frame_payload([conditional_read])},
        source_text=conditional_read_text,
    )
    conditional_read_policy = SemanticPolicyCompiler().compile(conditional_read_frame)
    expect(
        conditional_read_policy.preferred_route == "ask_user"
        and conditional_read_policy.selected_probe is None,
        "an unresolved conditional probe ran immediately",
        conditional_read_policy.to_dict(),
    )
    implicit_condition_text = "修改 README 要等我点头。"
    implicit_condition_act = act(
        implicit_condition_text,
        act_id="a1",
        kind="workspace_task",
        goal="确认后修改 README",
        operation="edit_file",
        source=implicit_condition_text[:-1],
        semantic_target=target("file", "README"),
    )
    implicit_condition_act["modality"] = "conditional"
    implicit_condition_frame = TurnSemanticFrame.from_model_payload(
        {"semantic_frame": frame_payload([implicit_condition_act])},
        source_text=implicit_condition_text,
    )
    expect(
        "conditional_modality_missing_condition_structure"
        in semantic_frame_quality_issues(
            implicit_condition_frame,
            implicit_condition_text,
        ),
        "conditional modality without condition structure passed quality checks",
    )
    implicit_condition_policy = SemanticPolicyCompiler().compile(
        implicit_condition_frame
    )
    expect(
        implicit_condition_policy.preferred_route == "ask_user"
        and not implicit_condition_policy.allowed_effects,
        "conditional modality without a condition authorized execution",
        implicit_condition_policy.to_dict(),
    )

    unresolved_text = "把它删掉。"
    unresolved_act = act(
        unresolved_text,
        act_id="a1",
        kind="workspace_task",
        goal="删除所指对象",
        operation="delete_file",
        source="把它删掉",
        semantic_target=target("file", ""),
    )
    unresolved_act["referent"] = {
        "surface": "它",
        "resolved": "",
        "status": "unresolved",
        "candidates": [],
    }
    unresolved_frame = TurnSemanticFrame.from_model_payload(
        {"semantic_frame": frame_payload([unresolved_act])},
        source_text=unresolved_text,
    )
    expect(
        "unresolved_referent_missing_ambiguity_structure"
        in semantic_frame_quality_issues(unresolved_frame, unresolved_text),
        "an unresolved referent passed the frame quality boundary",
    )
    unresolved_policy = SemanticPolicyCompiler().compile(unresolved_frame)
    expect(
        unresolved_policy.preferred_route == "ask_user"
        and not unresolved_policy.allowed_effects,
        "an unresolved destructive referent was authorized",
        unresolved_policy.to_dict(),
    )

    attachment_text = "把当前附件转成 CSV 文件。"
    attachment_frame = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        attachment_text,
                        act_id="a1",
                        kind="request",
                        goal="把当前附件转换为 CSV",
                        operation="convert_attachment_to_csv",
                        source="把当前附件转成 CSV 文件",
                        semantic_target=target("attachment", "current_attachment"),
                    )
                ]
            )
        },
        source_text=attachment_text,
    )
    attachment_policy = SemanticPolicyCompiler().compile(attachment_frame)
    expect(
        attachment_policy.preferred_route == "agent"
        and {"agent.execute", "workspace.write"}.issubset(attachment_policy.allowed_effects),
        "an open artifact conversion operation fell back to direct answer",
        attachment_policy.to_dict(),
    )

    external_text = "把日报邮件发给客户。"
    external_frame = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        external_text,
                        act_id="a1",
                        kind="workspace_task",
                        goal="向客户发送日报邮件",
                        operation="send_email",
                        source="把日报邮件发给客户",
                        semantic_target=target("email", "daily_report"),
                    )
                ]
            )
        },
        source_text=external_text,
    )
    external_policy = SemanticPolicyCompiler().compile(external_frame)
    expect(
        external_policy.preferred_route == "ask_user"
        and "agent.execute" not in external_policy.allowed_effects
        and "external.write" in external_policy.denied_effects,
        "an unenforced external write reached the Agent route",
        external_policy.to_dict(),
    )
    for action_text, operation, target_type in (
        ("回复老板的邮件。", "reply_email", "email"),
        ("给 Alice 发个私信。", "dm_user", "user"),
        ("在 GitHub issue 上评论。", "comment_issue", "github_issue"),
        ("安排明天会议。", "schedule_meeting", "meeting"),
        ("把云盘文件分享给小王。", "share_cloud_file", "cloud_file"),
        ("邀请 Bob 加入项目。", "invite_collaborator", "project"),
        ("合并这个 PR。", "merge_pull_request", "pull_request"),
    ):
        for action_kind in ("workspace_task", "request"):
            action_frame = TurnSemanticFrame.from_model_payload(
                {
                    "semantic_frame": frame_payload(
                        [
                            act(
                                action_text,
                                act_id="a1",
                                kind=action_kind,
                                goal=action_text[:-1],
                                operation=operation,
                                source=action_text[:-1],
                                semantic_target=target(target_type, "current"),
                            )
                        ]
                    )
                },
                source_text=action_text,
            )
            action_policy = SemanticPolicyCompiler().compile(action_frame)
            expect(
                action_policy.preferred_route == "ask_user"
                and "agent.execute" not in action_policy.allowed_effects,
                "an unscoped non-local Agent action bypassed effect enforcement",
                {
                    "text": action_text,
                    "kind": action_kind,
                    "policy": action_policy.to_dict(),
                },
            )
    compose_email_text = "回复老板的邮件。"
    compose_email_frame = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        compose_email_text,
                        act_id="a1",
                        kind="request",
                        goal="撰写邮件回复",
                        operation="compose_email",
                        source=compose_email_text[:-1],
                        semantic_target=target("email", "老板"),
                    )
                ]
            )
        },
        source_text=compose_email_text,
    )
    compose_email_policy = SemanticPolicyCompiler().compile(compose_email_frame)
    expect(
        compose_email_policy.preferred_route == "ask_user"
        and "agent.execute" not in compose_email_policy.allowed_effects,
        "a compose-email request without local content silently became a direct template",
        compose_email_policy.to_dict(),
    )

    conflicting_speaker_text = "请重启 Veyra 服务。"
    conflicting_speaker_frame = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        conflicting_speaker_text,
                        act_id="a1",
                        kind="workspace_task",
                        goal="重启 Veyra 服务",
                        operation="restart",
                        source="请重启 Veyra 服务",
                        semantic_target=target("service", "Veyra"),
                        speaker="boss",
                        authority="direct_user",
                    )
                ]
            )
        },
        source_text=conflicting_speaker_text,
    )
    conflicting_speaker_policy = SemanticPolicyCompiler().compile(conflicting_speaker_frame)
    expect(
        conflicting_speaker_policy.preferred_route == "ask_user"
        and not conflicting_speaker_policy.allowed_effects,
        "conflicting speaker and authority fields authorized execution",
        conflicting_speaker_policy.to_dict(),
    )

    structured_external_text = "Aurora 的缓存一致性风险还缺哪些证据？"
    structured_external_frame = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        structured_external_text,
                        act_id="a1",
                        kind="information_request",
                        goal="了解 Aurora 缓存一致性风险的证据缺口",
                        operation="query",
                        source=structured_external_text,
                        semantic_target=target(
                            "project",
                            "Aurora",
                            anchor_candidate_token="actok_candidate_token",
                        ),
                        evidence_need="fresh_external",
                    )
                ]
            )
        },
        source_text=structured_external_text,
    )
    structured_external_policy = SemanticPolicyCompiler().compile(
        structured_external_frame
    )
    expect(
        structured_external_policy.preferred_route == "probe"
        and structured_external_policy.selected_probe == "search_probe",
        "control-field names contaminated structured probe selection",
        structured_external_policy.to_dict(),
    )

    for search_text, query in (
        ("查最新 time complexity 文章。", "time complexity"),
        ("搜索 process mining 新闻。", "process mining"),
        ("查 port forwarding 最新教程。", "port forwarding"),
        ("搜索 git 2.50 release notes。", "git 2.50 release notes"),
    ):
        search_frame = TurnSemanticFrame.from_model_payload(
            {
                "semantic_frame": frame_payload(
                    [
                        act(
                            search_text,
                            act_id="a1",
                            kind="request",
                            goal=f"搜索 {query}",
                            operation="external_search",
                            source=search_text[:-1],
                            semantic_target=target("search_query", query),
                            evidence_need="fresh_external_search",
                        )
                    ]
                )
            },
            source_text=search_text,
        )
        search_policy = SemanticPolicyCompiler().compile(search_frame)
        expect(
            search_policy.selected_probe == "search_probe",
            "search content keywords stole the structural search route",
            search_policy.to_dict(),
        )

    url_text = "读取 https://github.com/example/port-forwarding 的内容。"
    url_frame = TurnSemanticFrame.from_model_payload(
        {
            "semantic_frame": frame_payload(
                [
                    act(
                        url_text,
                        act_id="a1",
                        kind="read_request",
                        goal="读取指定网页",
                        operation="fetch_url",
                        source=url_text[:-1],
                        semantic_target=target(
                            "url",
                            "https://github.com/example/port-forwarding",
                        ),
                        evidence_need="fresh_external_url",
                    )
                ]
            )
        },
        source_text=url_text,
    )
    url_policy = SemanticPolicyCompiler().compile(url_frame)
    expect(
        url_policy.selected_probe == "web",
        "URL content keywords stole the structural web route",
        url_policy.to_dict(),
    )

    for negative_read_text in (
        "不查天气了",
        "算了，不看天气了",
        "先不看天气",
        "天气先放一放",
        "停一下天气查询",
        "不必查天气",
        "不必查看天气",
        "先不查看天气",
        "暂时不查看天气",
        "算了别查看天气",
        "老板让我查天气",
        "老板要求查看天气",
        "客户让我查看天气",
        "同事说查一下 git 状态",
        "同事说查看 git 状态",
        "文档里要求查看天气",
    ):
        degraded_policy = SemanticPolicyCompiler().compile(
            TurnSemanticFrame.fallback(negative_read_text)
        )
        expect(
            degraded_policy.preferred_route != "probe"
            and degraded_policy.selected_probe is None,
            "a degraded non-explicit read surface was converted into a probe",
            {"text": negative_read_text, "policy": degraded_policy.to_dict()},
        )
    positive_degraded_policy = SemanticPolicyCompiler().compile(
        TurnSemanticFrame.fallback("上海天气怎么样？")
    )
    expect(
        positive_degraded_policy.preferred_route == "probe"
        and positive_degraded_policy.selected_probe == "weather_probe",
        "the degraded safety boundary blocked a high-confidence positive question",
        positive_degraded_policy.to_dict(),
    )

    with TemporaryDirectory(prefix="veyra-missing-semantic-policy-") as tmp:
        store = WorldStateStore(Path(tmp))
        controller = VeyraController(CapabilityRegistry(store))
        legacy_agent = Decision(
            route=Route.AGENT,
            risk_level=RiskLevel.R1,
            reason="legacy route without semantic policy",
            needs_agent=True,
        )
        guarded, _ = controller.prepare(legacy_agent)
        expect(
            guarded.route == Route.ASK_USER and guarded.needs_agent is False,
            "a legacy Agent route without semantic policy failed open",
            guarded.to_dict(),
        )

    print(
        json.dumps(
            {
                "schema": "veyra.semantic_resolver_quality_smoke.v1",
                "status": "ok",
                "checks": [
                    "collapsed hard boundary rejected",
                    "complete multi-act frame accepted",
                    "single bounded repair succeeds",
                    "double-invalid output locks all execution",
                    "invalid read-only explanations retain a safe direct fallback",
                    "transport degradation remains distinct",
                    "long fallback inputs stay bounded and fail closed",
                    "quoted discourse markers ignored by structural scanner",
                    "lexicalized commitment cancellation remains valid",
                    "negative destructive surface remains fail-closed",
                    "verified direct-user id remains authoritative",
                    "read-only Agent remains delegable without workspace write",
                    "denial cannot be re-opened by explanation text",
                    "conditional action and probe stay gated",
                    "unresolved referents stay gated",
                    "open artifact conversion routes to Agent",
                    "unenforced external writes stay gated",
                    "open non-local Agent rewrites stay gated",
                    "speaker-authority conflicts stay gated",
                    "structured search and URL probes beat content keywords",
                    "degraded probes require high-confidence positive read acts",
                    "missing semantic policy fails closed before Agent execution",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
