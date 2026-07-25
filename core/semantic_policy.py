from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_DURABLE_EFFECTS = {
    "agent.execute",
    "workspace.write",
    "external.write",
    "memory.write",
    "profile.write",
    "commitment.mutate",
    "proactive.create",
}

_NON_ASSERTED_MODES = {
    "example",
    "hypothetical",
    "mentioned",
    "quotation",
    "quoted",
    "reported",
    "reported_speech",
}

_NEGATIVE_POLARITIES = {"negative", "negated", "prohibit", "prohibited"}

_DENIAL_KINDS = {"deny", "denial", "negative_constraint", "prohibit", "prohibition"}

_INFORMATION_KINDS = {
    "answer",
    "conversation",
    "explain",
    "information",
    "information_request",
    "question",
    "query",
    "read",
    "read_request",
    "status_query",
}

_EXECUTION_KINDS = {
    "action",
    "command",
    "execution",
    "implementation",
    "mutation",
    "task",
    "tool_request",
    "workspace_task",
}

_PREFERENCE_KINDS = {"preference", "preference_update", "response_preference"}
_PROFILE_KINDS = {"profile_update", "self_disclosure", "user_fact"}
_COMMITMENT_KINDS = {"commitment", "commitment_control", "goal_control", "schedule_control"}
_PROACTIVE_KINDS = {"proactive_request", "recurring_request", "reminder_request", "subscription_request"}


@dataclass(frozen=True, slots=True)
class SemanticPolicy:
    """A deterministic, least-privilege projection of an open semantic frame.

    The language model may describe goals and operations with open strings.  This
    object deliberately contains only the small set of effects that Veyra itself
    can authorize.  No model-provided route, risk, or state-effect value is
    trusted.
    """

    schema_version: str = "veyra.semantic_policy.v1"
    resolver_status: str = "missing"
    preferred_route: str = "direct_answer"
    route_reason: str = "read-only semantic default"
    selected_probe: str | None = None
    capability_arguments: dict[str, Any] = field(default_factory=dict)
    probe_requests: list[dict[str, Any]] = field(default_factory=list)
    canonical_goals: list[str] = field(default_factory=list)
    allowed_capabilities: list[str] = field(default_factory=lambda: ["native_answer"])
    allowed_effects: list[str] = field(default_factory=list)
    denied_effects: list[str] = field(default_factory=lambda: sorted(_DURABLE_EFFECTS))
    effect_authorized_act_ids: dict[str, list[str]] = field(default_factory=dict)
    requires_clarification: bool = False
    clarification_reason: str = ""
    authoritative_act_ids: list[str] = field(default_factory=list)
    ignored_act_ids: list[str] = field(default_factory=list)
    policy_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "resolver_status": self.resolver_status,
            "preferred_route": self.preferred_route,
            "route_reason": self.route_reason,
            "selected_probe": self.selected_probe,
            "capability_arguments": dict(self.capability_arguments),
            "probe_requests": [dict(item) for item in self.probe_requests],
            "canonical_goals": list(self.canonical_goals),
            "allowed_capabilities": list(self.allowed_capabilities),
            "allowed_effects": list(self.allowed_effects),
            "denied_effects": list(self.denied_effects),
            "effect_authorized_act_ids": {
                effect: list(act_ids)
                for effect, act_ids in self.effect_authorized_act_ids.items()
            },
            "requires_clarification": self.requires_clarification,
            "clarification_reason": self.clarification_reason,
            "authoritative_act_ids": list(self.authoritative_act_ids),
            "ignored_act_ids": list(self.ignored_act_ids),
            "policy_signals": list(self.policy_signals),
        }

    def allows_effect(self, effect: str) -> bool:
        return effect in self.allowed_effects and effect not in self.denied_effects

    def allows_capability(self, capability: str) -> bool:
        return capability in self.allowed_capabilities


class SemanticPolicyCompiler:
    """Compile a model-resolved frame into Veyra-owned execution authority."""

    def compile(self, frame: Any, *, current_user_id: str | None = None) -> SemanticPolicy:
        payload = _as_dict(frame)
        if not payload:
            return SemanticPolicy(
                resolver_status="missing",
                policy_signals=["semantic_policy:frame_missing", "semantic_policy:read_only_default"],
            )

        resolver_status = _token(payload.get("resolver_status") or "unknown")
        acts = [_as_dict(item) for item in _as_list(payload.get("acts"))]
        acts = [item for item in acts if item]
        ambiguities = [_as_dict(item) for item in _as_list(payload.get("ambiguities"))]
        relations = [_as_dict(item) for item in _as_list(payload.get("relations"))]

        allowed_effects: set[str] = set()
        allowed_capabilities: set[str] = {"native_answer"}
        denied_effects: set[str] = set(_DURABLE_EFFECTS)
        effect_authorized_act_ids: dict[str, list[str]] = {}
        authoritative_ids: list[str] = []
        ignored_ids: list[str] = []
        signals: list[str] = []
        selected_probe: str | None = None
        capability_arguments: dict[str, Any] = {}
        probe_requests: list[dict[str, Any]] = []
        canonical_goals: list[str] = []
        needs_agent = False
        requires_clarification = False
        clarification_reason = ""
        has_positive_effectful_candidate = False
        capability_sensitive_act_ids: set[str] = set()
        current_user_token = _token(current_user_id)

        for index, act in enumerate(acts):
            act_id = str(act.get("act_id") or f"act_{index + 1}")
            effect_family = self._effect_family(act)
            if effect_family != "read" or self._probe_for_act(act)[0]:
                capability_sensitive_act_ids.add(act_id)

            if self._speaker_authority_conflict(act, current_user_id=current_user_token):
                ignored_ids.append(act_id)
                requires_clarification = True
                clarification_reason = "speaker and authority fields conflict"
                signals.append("semantic_policy:speaker_authority_conflict")
                continue

            if self._is_non_asserted(act, current_user_id=current_user_token):
                ignored_ids.append(act_id)
                signals.append(
                    f"semantic_policy:ignored_{self._non_asserted_reason(act, current_user_id=current_user_token)}"
                )
                continue

            referent = _as_dict(act.get("referent"))
            referent_status = _token(referent.get("status"))
            if (
                referent_status in {"ambiguous", "unresolved"}
                and act_id in capability_sensitive_act_ids
            ):
                ignored_ids.append(act_id)
                requires_clarification = True
                clarification_reason = "the referenced target is unresolved"
                signals.append("semantic_policy:unresolved_referent")
                continue

            if self._is_denial(act):
                denied = self._effects_for_denial(act)
                denied_effects.update(denied)
                allowed_effects.difference_update(denied)
                authoritative_ids.append(act_id)
                signals.append("semantic_policy:explicit_denial")
                continue

            if effect_family != "read":
                has_positive_effectful_candidate = True

            if self._has_unresolved_condition(act):
                ignored_ids.append(act_id)
                requires_clarification = True
                clarification_reason = "conditional request needs a satisfied trigger before capability use"
                signals.append("semantic_policy:conditional_effect_unresolved")
                continue

            if not self._is_authoritative(
                act,
                effect_family=effect_family,
                current_user_id=current_user_token,
            ):
                ignored_ids.append(act_id)
                if effect_family != "read":
                    requires_clarification = True
                    clarification_reason = "state-changing request is not explicit enough to authorize"
                    signals.append("semantic_policy:insufficient_authority")
                continue

            authoritative_ids.append(act_id)
            goal = str(act.get("goal") or "").strip()
            if goal:
                canonical_goals.append(goal[:1200])
            if effect_family == "preference":
                self._allow(allowed_effects, denied_effects, "memory.write", "profile.write")
                self._bind_effects_to_act(
                    effect_authorized_act_ids,
                    act_id,
                    "memory.write",
                    "profile.write",
                )
                signals.append("semantic_policy:preference_authorized")
            elif effect_family == "profile":
                self._allow(allowed_effects, denied_effects, "profile.write")
                self._bind_effects_to_act(effect_authorized_act_ids, act_id, "profile.write")
                signals.append("semantic_policy:profile_write_authorized")
            elif effect_family == "commitment":
                self._allow(allowed_effects, denied_effects, "commitment.mutate")
                self._bind_effects_to_act(effect_authorized_act_ids, act_id, "commitment.mutate")
                signals.append("semantic_policy:commitment_authorized")
            elif effect_family == "proactive":
                self._allow(allowed_effects, denied_effects, "proactive.create", "commitment.mutate")
                self._bind_effects_to_act(
                    effect_authorized_act_ids,
                    act_id,
                    "proactive.create",
                    "commitment.mutate",
                )
                signals.append("semantic_policy:proactive_authorized")
            elif effect_family == "execution":
                if self._external_write_requested(act):
                    ignored_ids.append(act_id)
                    requires_clarification = True
                    clarification_reason = (
                        "external writes need a tool proxy that can enforce the semantic effect scope"
                    )
                    signals.append("semantic_policy:external_write_enforcement_required")
                    continue
                if not self._governed_agent_execution_supported(act):
                    ignored_ids.append(act_id)
                    requires_clarification = True
                    clarification_reason = (
                        "non-local Agent execution needs effect-scoped tool enforcement"
                    )
                    signals.append("semantic_policy:unscoped_agent_execution_denied")
                    continue
                self._allow(allowed_effects, denied_effects, "agent.execute")
                self._bind_effects_to_act(effect_authorized_act_ids, act_id, "agent.execute")
                if self._workspace_write_requested(act):
                    self._allow(allowed_effects, denied_effects, "workspace.write")
                    self._bind_effects_to_act(effect_authorized_act_ids, act_id, "workspace.write")
                allowed_capabilities.add("selected_agent_runtime")
                needs_agent = True
                signals.append("semantic_policy:execution_authorized")
                if "workspace.write" not in allowed_effects:
                    signals.append("semantic_policy:agent_read_only")
            else:
                probe, capability = self._probe_for_act(act)
                if probe and capability:
                    probe_arguments = self._arguments_for_act(act, probe)
                    selected_probe = selected_probe or probe
                    allowed_capabilities.add(capability)
                    if selected_probe == probe:
                        capability_arguments.update(probe_arguments)
                    probe_requests.append(
                        {
                            "act_id": act_id,
                            "probe": probe,
                            "capability": capability,
                            "arguments": probe_arguments,
                            "goal": goal[:1200],
                        }
                    )
                    signals.append(f"semantic_policy:probe_{probe}_authorized")

        ambiguity_act_ids = {
            str(act_id)
            for ambiguity in ambiguities
            for act_id in _as_list(ambiguity.get("affected_act_ids"))
            if isinstance(act_id, str)
        }
        if ambiguities and (
            bool(ambiguity_act_ids & capability_sensitive_act_ids)
            or (not ambiguity_act_ids and bool(capability_sensitive_act_ids))
        ):
            requires_clarification = True
            clarification_reason = clarification_reason or self._ambiguity_reason(ambiguities)
            signals.append("semantic_policy:unresolved_ambiguity")

        if self._has_unresolved_conflict(relations, authoritative_ids):
            requires_clarification = True
            clarification_reason = clarification_reason or "conflicting state-changing acts need clarification"
            signals.append("semantic_policy:conflicting_effects")

        degraded = resolver_status not in {"complete", "resolved", "validated"}
        invalid_output = resolver_status in {"invalid", "invalid_output", "malformed"}
        if degraded:
            if selected_probe and not any(
                str(act.get("act_id") or "") in authoritative_ids
                and self._probe_for_act(act)[0] == selected_probe
                and self._safe_degraded_probe_act(act, current_user_id=current_user_token)
                for act in acts
            ):
                requires_clarification = True
                clarification_reason = (
                    clarification_reason
                    or "degraded semantic resolution lacks a high-confidence positive read request"
                )
                signals.append("semantic_policy:degraded_probe_not_explicit")
            durable_authorized = bool(allowed_effects & _DURABLE_EFFECTS)
            if durable_authorized or has_positive_effectful_candidate:
                requires_clarification = True
                clarification_reason = clarification_reason or "semantic resolution is degraded; no side effect is authorized"
            allowed_effects.difference_update(_DURABLE_EFFECTS)
            denied_effects.update(_DURABLE_EFFECTS)
            effect_authorized_act_ids.clear()
            needs_agent = False
            allowed_capabilities.discard("selected_agent_runtime")
            signals.extend(["semantic_policy:resolver_degraded", "semantic_policy:durable_effects_denied"])
        if invalid_output:
            unsafe_invalid_output = bool(
                not acts
                or has_positive_effectful_candidate
                or selected_probe
                or requires_clarification
                or not all(self._safe_invalid_read_only_act(act) for act in acts)
            )
            selected_probe = None
            probe_requests.clear()
            allowed_effects.clear()
            denied_effects.update(_DURABLE_EFFECTS)
            effect_authorized_act_ids.clear()
            if unsafe_invalid_output:
                requires_clarification = True
                clarification_reason = (
                    clarification_reason
                    or "semantic output remained invalid after one bounded repair"
                )
                allowed_capabilities = {"ask_user"}
                signals.append("semantic_policy:invalid_output_execution_locked")
            else:
                requires_clarification = False
                clarification_reason = ""
                allowed_capabilities = {"native_answer"}
                signals.append("semantic_policy:invalid_output_read_only_fallback")

        if requires_clarification:
            preferred_route = "ask_user"
            route_reason = clarification_reason or "unresolved semantic authority"
            selected_probe = None
            probe_requests.clear()
            needs_agent = False
            allowed_capabilities = {"ask_user"}
            allowed_effects.difference_update(_DURABLE_EFFECTS)
            denied_effects.update(_DURABLE_EFFECTS)
            effect_authorized_act_ids.clear()
        elif needs_agent:
            preferred_route = "agent"
            route_reason = "an explicit direct-user act authorizes governed execution"
        elif selected_probe:
            preferred_route = "probe"
            route_reason = f"an explicit information act requires fresh evidence via {selected_probe}"
        else:
            preferred_route = "direct_answer"
            route_reason = (
                "invalid semantic output is limited to a read-only core response"
                if invalid_output
                else "resolved acts require no external side effect"
            )

        return SemanticPolicy(
            resolver_status=resolver_status or "unknown",
            preferred_route=preferred_route,
            route_reason=route_reason,
            selected_probe=selected_probe,
            capability_arguments=capability_arguments,
            probe_requests=probe_requests,
            canonical_goals=_dedupe(canonical_goals),
            allowed_capabilities=sorted(allowed_capabilities),
            allowed_effects=sorted(allowed_effects),
            denied_effects=sorted(denied_effects),
            effect_authorized_act_ids={
                effect: _dedupe(act_ids)
                for effect, act_ids in sorted(effect_authorized_act_ids.items())
                if effect in allowed_effects
            },
            requires_clarification=requires_clarification,
            clarification_reason=clarification_reason,
            authoritative_act_ids=_dedupe(authoritative_ids),
            ignored_act_ids=_dedupe(ignored_ids),
            policy_signals=_dedupe(signals) or ["semantic_policy:read_only_default"],
        )

    def _effect_family(self, act: dict[str, Any]) -> str:
        kind = _token(act.get("kind"))
        operation_token = _token(act.get("operation"))
        operation = _joined(act.get("operation"), act.get("goal"), act.get("target"))
        if self._looks_like_external_execution(act):
            return "execution"
        if kind in _INFORMATION_KINDS:
            return "read"
        if kind in _EXECUTION_KINDS:
            return "execution"
        if self._is_read_operation(act):
            return "read"
        if kind in _PREFERENCE_KINDS or _contains(operation, "preference", "偏好", "回答风格", "language preference"):
            return "preference"
        if kind in _PROFILE_KINDS:
            return "profile"
        if kind in _PROACTIVE_KINDS or _contains(operation, "recurring", "subscribe", "subscription", "remind", "提醒", "订阅", "持续关注"):
            return "proactive"
        if operation_token in {
            "cancel",
            "cancel_task",
            "decline",
            "pause",
            "pause_task",
            "resume",
            "resume_task",
        }:
            return "commitment"
        if kind in _COMMITMENT_KINDS or _contains(
            operation,
            "commitment",
            "cancel_task",
            "pause_task",
            "resume_task",
            "goal.cancel",
            "goal.pause",
            "goal.resume",
            "任务取消",
            "暂停任务",
            "恢复任务",
        ):
            return "commitment"
        if self._looks_like_artifact_execution(act):
            return "execution"
        if _contains(
            operation,
            "add_component",
            "deploy",
            "edit_code",
            "implement",
            "modify_code",
            "repair_runtime",
            "write_file",
            "部署",
            "实现",
            "修改代码",
            "修复",
            "写入文件",
        ):
            return "execution"
        if self._probe_for_act(act)[0]:
            return "read"
        if _contains(
            operation,
            "build",
            "change",
            "create",
            "delete",
            "deploy",
            "drop",
            "edit",
            "execute",
            "fix",
            "implement",
            "modify",
            "remove",
            "restart",
            "restore",
            "rollback",
            "refactor",
            "repair",
            "run",
            "service_control",
            "start_service",
            "stop_service",
            "truncate",
            "write",
            "add_component",
            "add",
            "创建",
            "删除",
            "修改",
            "执行",
            "实现",
            "部署",
            "修复",
            "重构",
            "重启",
            "回滚",
            "恢复快照",
            "启动服务",
            "停止服务",
            "清空",
            "写入",
            "添加",
            "新增",
        ):
            return "execution"
        return "read"

    def _is_non_asserted(
        self,
        act: dict[str, Any],
        *,
        current_user_id: str = "",
    ) -> bool:
        mode = self._mention_mode(act)
        if mode in _NON_ASSERTED_MODES:
            return True
        modality = _token(act.get("modality"))
        if modality in {"counterfactual", "hypothetical", "possible"}:
            return True
        authority = _token(act.get("authority"))
        speaker = _token(act.get("speaker"))
        user_speakers = {"", "current_user", "requester", "user"}
        if current_user_id:
            user_speakers.add(current_user_id)
        if (
            authority in {"current_user", "direct_user", "requester", "user", "user_authorized"}
            and speaker in user_speakers
        ):
            return False
        if speaker and speaker not in user_speakers:
            return True
        return authority in {"denied", "external", "none", "non_authoritative", "quoted_speaker", "third_party"}

    def _is_denial(self, act: dict[str, Any]) -> bool:
        kind = _token(act.get("kind"))
        polarity = _token(act.get("polarity"))
        mode = self._mention_mode(act)
        return kind in _DENIAL_KINDS or polarity in _NEGATIVE_POLARITIES or mode in {"negated", "prohibited"}

    def _is_authoritative(
        self,
        act: dict[str, Any],
        *,
        effect_family: str,
        current_user_id: str = "",
    ) -> bool:
        if self._is_non_asserted(act, current_user_id=current_user_id) or self._is_denial(act):
            return False
        if effect_family == "read":
            return True
        explicitness = _token(act.get("explicitness"))
        authority = _token(act.get("authority"))
        speaker = _token(act.get("speaker"))
        source_quote = str(act.get("source_quote") or "").strip()
        explicit = explicitness in {"direct", "explicit", "user_explicit"}
        user_speakers = {"", "current_user", "requester", "user"}
        if current_user_id:
            user_speakers.add(current_user_id)
        user_speaker = speaker in user_speakers
        user_authority = (
            authority in {"current_user", "direct_user", "requester", "user", "user_authorized"}
            and user_speaker
        ) or (not authority and speaker in user_speakers)
        return bool(explicit and user_authority and source_quote)

    def _speaker_authority_conflict(
        self,
        act: dict[str, Any],
        *,
        current_user_id: str = "",
    ) -> bool:
        authority = _token(act.get("authority"))
        speaker = _token(act.get("speaker"))
        user_speakers = {"", "current_user", "requester", "user"}
        if current_user_id:
            user_speakers.add(current_user_id)
        return (
            authority in {"current_user", "direct_user", "requester", "user", "user_authorized"}
            and speaker not in user_speakers
        )

    def _safe_degraded_probe_act(
        self,
        act: dict[str, Any],
        *,
        current_user_id: str = "",
    ) -> bool:
        kind = _token(act.get("kind"))
        explicitness = _token(act.get("explicitness"))
        return (
            kind
            in {
                "information_request",
                "query",
                "question",
                "read_request",
                "request",
                "statement",
                "status_query",
            }
            and explicitness
            in {
                "explicit",
                "inferred",
                "strong_implied",
                "user_explicit",
                "weak_implied",
            }
            and not self._is_non_asserted(act, current_user_id=current_user_id)
            and not self._is_denial(act)
            and not self._has_unresolved_condition(act)
            and self._degraded_source_is_direct_positive(act)
        )

    def _safe_invalid_read_only_act(self, act: dict[str, Any]) -> bool:
        if self._is_denial(act) or self._is_non_asserted(act):
            return True
        kind = _token(act.get("kind"))
        operation = _token(act.get("operation"))
        source = _as_dict(act.get("source_quote"))
        text = str(source.get("text") or "").strip()
        if not text:
            return False
        if operation == "understand_open_goal":
            return self._invalid_open_goal_is_conversational(text)
        if operation == "answer_question":
            return self._invalid_surface_is_information_request(text)
        if self._is_read_operation(act):
            return True
        if kind in {"acknowledgement", "conversation", "feedback", "greeting"}:
            return not self._invalid_surface_suggests_effect(text)
        if kind in _INFORMATION_KINDS:
            return self._invalid_surface_is_information_request(text)
        return False

    @staticmethod
    def _invalid_surface_is_information_request(text: str) -> bool:
        lowered = text.lower().strip()
        read_cue = bool(
            re.search(
                r"(?:为什么|为何|什么|怎么|如何|哪(?:个|些|里)?|谁|何时|什么时候|"
                r"解释|说明|告诉我|分析|比较|对比|总结|是否|是不是|有没有|含义|意思)",
                text,
            )
            or re.search(
                r"\b(?:what|why|how|who|when|where|which|explain|describe|"
                r"tell me|analy[sz]e|compare|summari[sz]e|meaning|mean)\b",
                lowered,
            )
        )
        capability_question = bool(
            re.match(r"^(?:你能|你可以|能不能|可不可以|是否可以|可否)", text)
            or re.match(r"^(?:can|could|would|will)\s+you\b", lowered)
        )
        return read_cue or (
            not capability_question
            and bool(
                re.match(r"^(?:is|are|am|do|does|did|has|have|was|were)\b", lowered)
                or re.search(r"(?:吗|呢|怎么样)[？?]?$", text)
            )
        )

    @staticmethod
    def _invalid_surface_suggests_effect(text: str) -> bool:
        lowered = text.lower().strip()
        return bool(
            re.search(
                r"(?:把|将).{0,100}(?:做成|变成|转成|转换|整理成|生成|导出|保存|"
                r"修改|删除|创建|发送|发给|回复|上传|分享|邀请|合并|部署|运行|"
                r"执行|重启|写入|添加|新增)",
                text,
            )
            or re.search(
                r"(?:附件|文件|代码|仓库|项目|数据集|表格|文档|邮件|消息|日历|"
                r"会议|issue|工单).{0,60}(?:一下|吧|给我|替我|帮我|处理|折叠|"
                r"转换|修改|删除|创建|发送|回复|上传|分享|邀请|合并|部署|运行|执行)",
                text,
                re.IGNORECASE,
            )
            or re.search(
                r"\b(?:turn|convert|transform|make|render|export|save|write|edit|"
                r"delete|remove|create|send|reply|upload|share|invite|merge|deploy|"
                r"run|execute|restart)\b.{0,100}\b(?:attachment|file|code|repository|"
                r"project|dataset|spreadsheet|document|email|message|calendar|meeting|"
                r"issue|ticket|archive)\b",
                lowered,
            )
        )

    def _invalid_open_goal_is_conversational(self, text: str) -> bool:
        stripped = text.strip()
        lowered = stripped.lower()
        if self._invalid_surface_suggests_effect(stripped):
            return False
        if re.fullmatch(
            r"(?:你好|您好|嗨|哈喽|谢谢|多谢|好的|好|明白了|知道了|收到|再见)[！!。.]?",
            stripped,
        ) or re.fullmatch(
            r"(?:hi|hello|hey|thanks|thank you|ok|okay|got it|bye)[!.]?",
            lowered,
        ):
            return True
        if re.match(
            r"^(?:我觉得|我认为|我感觉|在我看来|我喜欢|我不喜欢|这个|这套|这个方案|这件事)",
            stripped,
        ):
            return True
        return bool(
            re.match(r"^(?:i think|i feel|i believe|i like|i dislike|in my view)\b", lowered)
            or re.search(r"(?:很好|不错|不好|很差|有问题|有帮助|没效果|没有效果)[。.!！]?$", stripped)
        )

    def _degraded_source_is_direct_positive(self, act: dict[str, Any]) -> bool:
        source = _as_dict(act.get("source_quote"))
        text = str(source.get("text") or "").strip()
        lowered = text.lower()
        if not text:
            return False
        if re.search(
            r"(?:不必|不用|不要|无需|不再|先不|暂时不|暂不|先别|别再|算了|停止|停一下|放一放|取消)",
            text,
        ) or re.search(r"\b(?:do not|don't|dont|never|stop|cancel|not now)\b", lowered):
            return False
        if re.search(
            r"(?:老板|领导|同事|客户|对方|他|她|他们|日志|文档|网页|文章|系统)"
            r"(?:里|中|上)?(?:说|写着|要求|提示|让我|让你|建议|提到|显示)",
            text,
        ) or re.search(
            r"\b(?:boss|manager|client|colleague|he|she|they|document|log|page|article)"
            r"\s+(?:said|asked|told|says|shows|requires)\b",
            lowered,
        ):
            return False
        compact = re.sub(r"[\s，,。！？!?、]+", "", text)
        if compact in {
            "没有",
            "没有这些",
            "都没有",
            "不是这些",
            "没这些",
            "换一批",
            "再找",
            "继续找",
            "重新搜",
            "重新找",
        } and _token(act.get("operation")) == "retry_external_search":
            return True
        return bool(
            re.match(
                r"^(?:请|请问|帮我|麻烦|劳驾|查|查询|搜索|搜|看看|看下|查看|读取|打开)",
                text,
            )
            or re.search(r"(?:是不是|有没有|是否)", text)
            or re.search(r"(?:吗|呢|怎么样|如何|多少|几点|什么)[？?]?$", text)
            or re.search(r"[？?]$", text)
            or re.match(
                r"^(?:please\b|can you\b|could you\b|would you\b|check\b|search\b|look up\b|"
                r"read\b|open\b|what\b|when\b|where\b|how\b|is\b|are\b)",
                lowered,
            )
        )

    def _governed_agent_execution_supported(self, act: dict[str, Any]) -> bool:
        target = _as_dict(act.get("target"))
        target_type = _token(target.get("type"))
        operation = _token(act.get("operation"))
        local_targets = {
            "artifact",
            "attachment",
            "code",
            "codebase",
            "command",
            "component",
            "config",
            "configuration",
            "dataset",
            "directory",
            "document",
            "file",
            "function",
            "image",
            "local_runtime",
            "local_system",
            "module",
            "package",
            "process",
            "project",
            "repository",
            "runtime",
            "script",
            "service",
            "spreadsheet",
            "terminal",
            "test_suite",
            "workspace",
        }
        if target_type not in local_targets:
            return False
        if self._workspace_write_requested(act) or self._is_read_operation(act):
            return True
        return _contains(
            operation,
            "build",
            "execute_command",
            "restart",
            "run_command",
            "run_script",
            "run_test",
            "service_control",
            "start_service",
            "stop_service",
            "启动服务",
            "停止服务",
            "执行命令",
            "运行测试",
            "重启",
        )

    def _has_unresolved_condition(self, act: dict[str, Any]) -> bool:
        modality = _token(act.get("modality"))
        if modality in {
            "conditional",
            "contingent",
            "pending_approval",
            "pending_confirmation",
        }:
            return True
        condition = act.get("condition")
        if condition in (None, "", {}, []):
            return False
        if isinstance(condition, str):
            return _token(condition) not in {"always", "none", "unconditional"}
        if isinstance(condition, dict):
            return not bool(condition.get("resolved") or condition.get("satisfied"))
        return True

    def _effects_for_denial(self, act: dict[str, Any]) -> set[str]:
        family = self._effect_family(act)
        if family == "preference":
            return {"memory.write", "profile.write"}
        if family == "profile":
            return {"profile.write"}
        if family == "commitment":
            return {"commitment.mutate"}
        if family == "proactive":
            return {"proactive.create", "commitment.mutate"}
        if family == "execution":
            operation = _joined(
                act.get("kind"),
                act.get("operation"),
                act.get("goal"),
                act.get("target"),
            )
            if _contains(
                operation,
                "do_not_execute",
                "dont_execute",
                "execute_nothing",
                "prevent_execution",
                "不要执行",
                "别执行",
                "禁止执行",
            ) or _token(act.get("operation")) == "execute":
                return {"agent.execute", "workspace.write", "external.write"}
            denied: set[str] = set()
            if self._workspace_write_requested(act):
                denied.add("workspace.write")
            if _contains(
                operation,
                "email",
                "message",
                "notify_external",
                "post",
                "publish",
                "send",
                "邮件",
                "消息",
                "发布",
                "发送",
            ):
                denied.add("external.write")
            if _contains(
                operation,
                "agent",
                "delegate",
                "dispatch",
                "handoff",
                "不要让",
                "别让",
            ):
                denied.add("agent.execute")
            return denied or {"agent.execute", "workspace.write", "external.write"}
        return set()

    def _workspace_write_requested(self, act: dict[str, Any]) -> bool:
        operation = _joined(
            act.get("kind"),
            act.get("operation"),
            act.get("goal"),
            act.get("target"),
        )
        return _contains(
            operation,
            "add_component",
            "change_code",
            "create_file",
            "convert_attachment",
            "convert_file",
            "delete_file",
            "delete_repository",
            "edit_code",
            "edit_file",
            "fix_bug",
            "implement",
            "export_file",
            "generate_artifact",
            "generate_file",
            "import_file",
            "modify_code",
            "patch_code",
            "refactor",
            "remove_file",
            "rename_file",
            "repair_code",
            "write_file",
            "attachment_to",
            "document_to",
            "file_to",
            "修改代码",
            "修改文件",
            "删除仓库",
            "删除文件",
            "写代码",
            "写入文件",
            "实现功能",
            "修复代码",
            "修复bug",
            "重构",
            "新增组件",
            "添加组件",
            "创建文件",
            "转换附件",
            "转换文件",
            "转成csv",
            "转为csv",
            "导出文件",
            "生成文件",
            "保存文件",
        )

    def _external_write_requested(self, act: dict[str, Any]) -> bool:
        operation = _joined(
            act.get("kind"),
            act.get("operation"),
            act.get("goal"),
            act.get("target"),
        )
        return _contains(
            operation,
            "create_calendar_event",
            "create_issue_comment",
            "comment_issue",
            "dm_user",
            "external_write",
            "invite_collaborator",
            "merge_pull_request",
            "post_message",
            "publish",
            "reply_email",
            "reply_message",
            "schedule_meeting",
            "send_email",
            "send_message",
            "send_notification",
            "share_cloud_file",
            "submit_form",
            "upload",
            "回复邮件",
            "回复消息",
            "评论issue",
            "评论工单",
            "安排会议",
            "创建会议",
            "分享云盘",
            "邀请成员",
            "邀请协作者",
            "合并pr",
            "发邮件",
            "发送邮件",
            "发送消息",
            "发消息",
            "发布",
            "提交表单",
            "上传",
        )

    def _looks_like_external_execution(self, act: dict[str, Any]) -> bool:
        if self._is_read_operation(act):
            return False
        kind = _token(act.get("kind"))
        operation = _token(act.get("operation"))
        target = _as_dict(act.get("target"))
        target_type = _token(target.get("type"))
        if self._external_write_requested(act):
            return True
        external_targets = {
            "calendar",
            "calendar_event",
            "channel",
            "chat",
            "cloud_file",
            "email",
            "external_service",
            "github_issue",
            "issue",
            "meeting",
            "message",
            "pull_request",
            "recipient",
            "slack",
            "ticket",
            "user",
        }
        request_kinds = _EXECUTION_KINDS | {
            "instruction",
            "request",
            "user_request",
        }
        return bool(
            target_type in external_targets
            and kind in request_kinds
            and operation
            and operation not in {"none", "understand_open_goal"}
        )

    def _is_read_operation(self, act: dict[str, Any]) -> bool:
        operation = _token(act.get("operation"))
        if not operation:
            return False
        if operation in {
            "analyze",
            "answer",
            "compare",
            "describe",
            "explain",
            "inspect",
            "query",
            "read",
            "review",
            "summarize",
            "what",
            "why",
            "分析",
            "回答",
            "比较",
            "对比",
            "解释",
            "查看",
            "查询",
            "读取",
            "审查",
            "总结",
            "说明",
        }:
            return True
        return operation.startswith(
            (
                "analyze_",
                "answer_",
                "compare_",
                "describe_",
                "explain_",
                "inspect_",
                "query_",
                "read_",
                "review_",
                "summarize_",
                "what_",
                "why_",
                "分析",
                "回答",
                "比较",
                "对比",
                "解释",
                "查看",
                "查询",
                "读取",
                "审查",
                "总结",
                "说明",
            )
        )

    def _looks_like_artifact_execution(self, act: dict[str, Any]) -> bool:
        kind = _token(act.get("kind"))
        if kind not in {"instruction", "request", "user_request"}:
            return False
        target = _as_dict(act.get("target"))
        target_type = _token(target.get("type"))
        operation = _token(act.get("operation"))
        if self._is_read_operation(act):
            return False
        artifact_targets = {
            "artifact",
            "attachment",
            "codebase",
            "dataset",
            "document",
            "file",
            "image",
            "repository",
            "spreadsheet",
            "workspace",
        }
        if target_type not in artifact_targets:
            return False
        return _contains(
            operation,
            "convert",
            "create",
            "edit",
            "export",
            "generate",
            "import",
            "modify",
            "render",
            "save",
            "transform",
            "write",
            "转换",
            "创建",
            "导出",
            "生成",
            "保存",
            "修改",
        )

    def _probe_for_act(self, act: dict[str, Any]) -> tuple[str | None, str | None]:
        evidence_need = _token(act.get("evidence_need"))
        operation = _token(act.get("operation"))
        target = _as_dict(act.get("target"))
        target_type = _token(target.get("type"))
        arguments = _as_dict(act.get("arguments"))
        requires_observation = (
            evidence_need not in {"", "context", "none", "unknown"}
            or operation.startswith("query_current")
            or operation.startswith("query_fresh")
            or operation in {"query_runtime_status", "query_status"}
        )
        if not requires_observation:
            return None, None
        if (
            target_type in {"url", "web_page", "web_url", "webpage"}
            or operation in {"fetch_url", "open_url", "query_url", "read_url"}
            or operation.startswith(("fetch_url_", "open_url_", "query_url_", "read_url_"))
            or evidence_need in {"external_url", "fresh_external_url", "url"}
            or bool(str(arguments.get("url") or "").strip())
        ):
            return "web", "web_url_probe"
        if (
            operation in {"external_search", "search_external", "web_search"}
            or operation.startswith(("external_search_", "search_external_", "web_search_"))
            or target_type in {"search", "search_query", "web_search"}
            or evidence_need in {"external_search", "fresh_external_search", "web_search"}
        ):
            return "search_probe", "web_search"
        evidence = _joined(act.get("evidence_need"), act.get("target"), act.get("operation"), act.get("goal"))
        if not evidence:
            return None, None
        if _contains(evidence, "weather", "天气", "气温", "temperature"):
            return "weather_probe", "weather_probe"
        if _contains(evidence, "openclaw"):
            return "openclaw", "openclaw_probe"
        if _contains(evidence, "hermes"):
            return "hermes", "hermes_probe"
        if _contains(evidence, "mcp"):
            return "mcp", "mcp_probe"
        if _contains(evidence, "git", "working tree", "工作区", "脏文件", "未提交"):
            return "git", "git_probe"
        if _contains(evidence, "port", "端口"):
            return "port", "port_probe"
        if _contains(evidence, "process", "进程"):
            return "process", "process_probe"
        if _contains(evidence, "runtime", "process", "port", "service status", "运行态", "进程", "端口", "服务状态"):
            return "system", "system_probe"
        if _contains(evidence, "clock", "current time", "date", "time", "today", "日期", "几点", "时间", "今天"):
            return "time", "time_probe"
        if _contains(evidence, "url", "web page", "网页", "链接"):
            return "web", "web_url_probe"
        if _contains(evidence, "current external", "latest", "news", "search", "web search", "外部", "最新", "新闻", "搜索"):
            return "search_probe", "web_search"
        return None, None

    def _arguments_for_act(self, act: dict[str, Any], probe: str) -> dict[str, Any]:
        target = _as_dict(act.get("target"))
        arguments = _as_dict(act.get("arguments"))
        value = str(target.get("value") or "").strip()
        if probe == "weather_probe":
            location = str(
                arguments.get("location")
                or arguments.get("place")
                or arguments.get("city")
                or value
                or ""
            ).strip()
            return {"location": location} if location else {}
        if probe == "search_probe":
            query = str(arguments.get("query") or arguments.get("search_query") or value or act.get("goal") or "").strip()
            return {"query": query[:1200]} if query else {}
        if probe == "web":
            url = str(arguments.get("url") or value or "").strip()
            return {"url": url} if url else {}
        if probe == "port":
            attributes = target.get("attributes") if isinstance(target.get("attributes"), dict) else {}
            port = arguments.get("port") or attributes.get("port")
            if port is None and value.isdigit():
                port = int(value)
            return {"port": port} if port is not None else {}
        return arguments

    def _mention_mode(self, act: dict[str, Any]) -> str:
        return _token(act.get("mention_mode") or "asserted")

    def _non_asserted_reason(
        self,
        act: dict[str, Any],
        *,
        current_user_id: str = "",
    ) -> str:
        modality = _token(act.get("modality"))
        authority = _token(act.get("authority"))
        speaker = _token(act.get("speaker"))
        user_speakers = {"", "current_user", "requester", "user"}
        if current_user_id:
            user_speakers.add(current_user_id)
        direct_user_authority = authority in {
            "current_user",
            "direct_user",
            "requester",
            "user",
            "user_authorized",
        } and speaker in user_speakers
        if modality == "reported" or "reported" in authority or (
            not direct_user_authority
            and speaker
            and speaker not in user_speakers
        ):
            return "reported_speech"
        if modality in {"counterfactual", "hypothetical", "possible"}:
            return "hypothetical"
        return self._mention_mode(act)

    def _ambiguity_reason(self, ambiguities: list[dict[str, Any]]) -> str:
        reasons: list[str] = []
        for item in ambiguities:
            question = str(item.get("question") or item.get("description") or item.get("reason") or "").strip()
            if question:
                reasons.append(question)
        if reasons:
            return "；".join(reasons[:3])[:500]
        return "state-changing request has unresolved ambiguity"

    def _has_unresolved_conflict(self, relations: list[dict[str, Any]], authoritative_ids: list[str]) -> bool:
        active = set(authoritative_ids)
        for relation in relations:
            relation_type = _token(relation.get("kind") or relation.get("type") or relation.get("relation"))
            if relation_type not in {"conflict", "contradiction", "mutually_exclusive"}:
                continue
            source = str(relation.get("source_act_id") or relation.get("from_act_id") or "")
            target = str(relation.get("target_act_id") or relation.get("to_act_id") or "")
            if not source or not target or source in active or target in active:
                return True
        return False

    def _allow(self, allowed: set[str], denied: set[str], *effects: str) -> None:
        allowed.update(effects)
        denied.difference_update(effects)

    @staticmethod
    def _bind_effects_to_act(
        bindings: dict[str, list[str]],
        act_id: str,
        *effects: str,
    ) -> None:
        for effect in effects:
            bindings.setdefault(effect, []).append(act_id)


def semantic_policy_for(frame: Any) -> SemanticPolicy:
    return SemanticPolicyCompiler().compile(frame)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        payload = dump(mode="json")
        return payload if isinstance(payload, dict) else {}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return payload if isinstance(payload, dict) else {}
    return {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _joined(*values: Any) -> str:
    return " ".join(str(value or "").strip().lower() for value in values if value not in (None, "", {}, []))


def _contains(text: str, *markers: str) -> bool:
    return any(marker.lower() in text for marker in markers)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
