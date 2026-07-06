from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence
from uuid import uuid4


ENTITY_PATTERN = r"[A-Za-z][A-Za-z0-9_.+#-]*(?:\s*\d+(?:\.\d+)*)?"


@dataclass(frozen=True, slots=True)
class EntityMention:
    name: str
    role: str
    kind: str = "topic"


@dataclass(frozen=True, slots=True)
class SemanticEvent:
    speech_act: str
    entities: list[EntityMention] = field(default_factory=list)
    modality: str = "certain"
    temporal_scope: str = "unknown"
    commitment_strength: float = 0.0
    confidence: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class StateChange:
    entity: str
    state_field: str
    operation: str
    direction: str = ""
    new_value: str = ""
    confidence: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ProposedAction:
    action: str
    target: str
    requires_confirmation: bool = True
    reason: str = ""
    commitment_id: str = ""
    commitment_kind: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    mode: str
    reason: str


@dataclass(frozen=True, slots=True)
class SemanticChangeSet:
    changeset_id: str
    type: str
    semantic_event: SemanticEvent
    changes: list[StateChange]
    proposed_actions: list[ProposedAction]
    execution_decision: ExecutionDecision
    source_text: str
    status: str = "pending_confirmation"
    source: str = "semantic_changeset_compiler"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_semantic_change_set(
    text: str,
    *,
    active_commitments: Sequence[dict[str, Any]] | None = None,
) -> SemanticChangeSet | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    compact = _compact(raw)
    if _looks_like_state_query(compact, lowered):
        return None
    if _looks_like_direct_single_action(compact, lowered):
        return None

    commitments = list(active_commitments or [])
    changes: list[StateChange] = []
    proposed: list[ProposedAction] = []
    entities: list[EntityMention] = []

    old_topic, new_topic = _extract_shift_pair(raw)
    if old_topic:
        entities.append(EntityMention(old_topic, "decreasing_focus"))
        changes.append(
            StateChange(
                entity=old_topic,
                state_field="project_relevance",
                operation="decrease",
                direction="down",
                confidence=0.78,
                reason="user expressed lower fit or priority for this topic",
            )
        )
        existing = _matching_commitment(commitments, old_topic, kinds={"external_digest", "learning_digest"})
        if existing:
            proposed.append(
                ProposedAction(
                    action="deprioritize_tracking",
                    target=old_topic,
                    commitment_id=str(existing.get("commitment_id") or ""),
                    commitment_kind=str(existing.get("kind") or ""),
                    reason="existing commitment matches a topic whose relevance appears to be decreasing",
                )
            )
    if new_topic:
        entities.append(EntityMention(new_topic, "increasing_focus"))
        field = "learning_interest" if _mentions_learning(raw, lowered) else "project_relevance"
        changes.append(
            StateChange(
                entity=new_topic,
                state_field=field,
                operation="increase",
                direction="up",
                confidence=0.82,
                reason="user expressed higher interest or fit for this topic",
            )
        )
        proposed.append(
            ProposedAction(
                action="create_learning_tracking" if field == "learning_interest" else "create_tracking",
                target=new_topic,
                reason="new topic appears to be a candidate for future learning or tracking",
            )
        )

    preferred_topic = _extract_preferred_topic(raw, lowered)
    if preferred_topic and preferred_topic not in {change.entity for change in changes}:
        entities.append(EntityMention(preferred_topic, "preference_signal"))
        changes.append(
            StateChange(
                entity=preferred_topic,
                state_field="project_relevance",
                operation="increase",
                direction="up",
                confidence=0.68,
                reason="user expressed a soft preference signal without asking to create tracking",
            )
        )

    tentative_learning = _extract_tentative_learning_topic(raw, lowered)
    if tentative_learning and tentative_learning not in {change.entity for change in changes}:
        entities.append(EntityMention(tentative_learning, "learning_candidate"))
        changes.append(
            StateChange(
                entity=tentative_learning,
                state_field="learning_interest",
                operation="increase",
                direction="up",
                confidence=0.76,
                reason="user expressed a tentative learning intention",
            )
        )
        proposed.append(
            ProposedAction(
                action="create_learning_tracking",
                target=tentative_learning,
                reason="learning interest is tentative and needs confirmation before becoming a commitment",
            )
        )

    decreased_topic = _extract_decreased_topic(raw, lowered)
    if decreased_topic and decreased_topic not in {change.entity for change in changes}:
        entities.append(EntityMention(decreased_topic, "decreasing_focus"))
        changes.append(
            StateChange(
                entity=decreased_topic,
                state_field="project_relevance",
                operation="decrease",
                direction="down",
                confidence=0.68,
                reason="user expressed lower importance without a direct control command",
            )
        )
        existing = _matching_commitment(commitments, decreased_topic, kinds={"external_digest", "learning_digest"})
        if existing:
            proposed.append(
                ProposedAction(
                    action="deprioritize_tracking",
                    target=decreased_topic,
                    commitment_id=str(existing.get("commitment_id") or ""),
                    commitment_kind=str(existing.get("kind") or ""),
                    reason="existing commitment matches a topic whose priority may have dropped",
                )
            )

    attention_topic = _extract_attention_topic(raw, lowered)
    if attention_topic and attention_topic not in {change.entity for change in changes}:
        entities.append(EntityMention(attention_topic, "attention_candidate"))
        changes.append(
            StateChange(
                entity=attention_topic,
                state_field="attention_interest",
                operation="increase",
                direction="up",
                confidence=0.68,
                reason="user expressed an attention signal rather than a subscription command",
            )
        )
        proposed.append(
            ProposedAction(
                action="create_tracking",
                target=attention_topic,
                reason="attention signal can become tracking only after confirmation",
            )
        )

    add_topic = _extract_add_topic(raw, lowered)
    if add_topic and add_topic not in {change.entity for change in changes}:
        entities.append(EntityMention(add_topic, "add_candidate"))
        changes.append(
            StateChange(
                entity=add_topic,
                state_field="tracking_interest",
                operation="increase",
                direction="up",
                confidence=0.78,
                reason="user mentioned adding a new topic while preserving existing state",
            )
        )
        proposed.append(
            ProposedAction(
                action="create_tracking",
                target=add_topic,
                reason="add request should be confirmed before creating tracking",
            )
        )

    kept_topic = _extract_keep_topic(raw, lowered)
    if kept_topic and kept_topic not in {change.entity for change in changes}:
        entities.append(EntityMention(kept_topic, "preserve_existing"))
        changes.append(
            StateChange(
                entity=kept_topic,
                state_field="tracking_status",
                operation="keep",
                new_value="unchanged",
                confidence=0.84,
                reason="user explicitly said not to cancel or stop this topic",
            )
        )

    held_topic = _extract_hold_subscription_topic(raw, lowered)
    if held_topic and held_topic not in {change.entity for change in changes}:
        entities.append(EntityMention(held_topic, "do_not_subscribe"))
        changes.append(
            StateChange(
                entity=held_topic,
                state_field="tracking_status",
                operation="hold",
                new_value="not_subscribed",
                confidence=0.82,
                reason="user explicitly said not to subscribe or track this topic yet",
            )
        )

    if not changes:
        return None

    proposed = _dedupe_actions(proposed)
    modality = _modality(raw, lowered)
    strength = _commitment_strength(raw, lowered, proposed)
    speech_act = _speech_act(changes=changes, proposed_actions=proposed, modality=modality)
    if proposed:
        mode = "ask_confirmation"
        reason = "semantic state changes imply possible task mutations, but the user did not grant direct execution"
        status = "pending_confirmation"
    else:
        mode = "store_belief_only"
        reason = "semantic state changes are useful as belief updates but do not imply an executable action"
        status = "recorded"
    return SemanticChangeSet(
        changeset_id=f"scs_{uuid4().hex[:12]}",
        type="semantic_change_set",
        semantic_event=SemanticEvent(
            speech_act=speech_act,
            entities=_dedupe_entities(entities),
            modality=modality,
            temporal_scope="future" if any(token in raw for token in ("接下来", "以后", "今后", "后面", "未来")) else "current",
            commitment_strength=strength,
            confidence=0.82 if proposed else 0.7,
            reason="compiled from semantic state-change signals",
        ),
        changes=changes,
        proposed_actions=proposed,
        execution_decision=ExecutionDecision(mode=mode, reason=reason),
        source_text=raw,
        status=status,
    )


def confirmation_message(changeset: dict[str, Any]) -> str:
    changes = changeset.get("changes") if isinstance(changeset.get("changes"), list) else []
    actions = changeset.get("proposed_actions") if isinstance(changeset.get("proposed_actions"), list) else []
    if not changes:
        return "我把这句话理解为状态变化信号，但还不能安全执行任何任务变更。"
    lines = ["我把这句话理解为状态变化候选："]
    for change in changes[:5]:
        if not isinstance(change, dict):
            continue
        entity = str(change.get("entity") or "这个主题")
        field = _field_label(str(change.get("state_field") or "state"))
        op = _operation_label(str(change.get("operation") or ""), str(change.get("direction") or ""), str(change.get("new_value") or ""))
        lines.append(f"- {entity}：{field}{op}")
    if actions:
        labels = [_action_label(action) for action in actions if isinstance(action, dict)]
        lines.append("")
        lines.append("这些会改变长期任务或关注状态，需要你确认后再执行：" + "；".join(labels[:5]) + "。")
        lines.append("回复「同意」我再执行；回复「不用」则只保留这次理解。")
    else:
        lines.append("")
        lines.append("我会把它作为偏好/状态信号记录，不会创建、取消或订阅任何任务。")
    return "\n".join(lines)


def confirmed_message(applied_actions: list[dict[str, Any]]) -> str:
    if not applied_actions:
        return "已确认，但这个候选变化没有需要执行的任务动作。"
    labels = []
    for item in applied_actions[:6]:
        action = str(item.get("action") or "")
        target = str(item.get("target") or "这个主题")
        status = str(item.get("status") or "")
        if action == "deprioritize_tracking" and status == "paused":
            labels.append(f"已暂停/降级 {target} 的相关追踪")
        elif action == "create_learning_tracking" and status in {"created", "already_exists"}:
            labels.append(f"已记录 {target} 学习追踪")
        elif action == "create_tracking" and status in {"created", "already_exists"}:
            labels.append(f"已记录 {target} 外部追踪")
        else:
            labels.append(f"{target}：{status or action}")
    return "已按确认处理：" + "；".join(labels) + "。"


def declined_message(changeset: dict[str, Any]) -> str:
    event = changeset.get("semantic_event") if isinstance(changeset.get("semantic_event"), dict) else {}
    entities = event.get("entities") if isinstance(event.get("entities"), list) else []
    names = [str(item.get("name") or "") for item in entities if isinstance(item, dict) and item.get("name")]
    if names:
        return "好的，不会应用这次关于 " + "、".join(names[:4]) + " 的状态变更。"
    return "好的，不会应用这次状态变更。"


def _looks_like_state_query(compact: str, lowered: str) -> bool:
    questionish = any(token in compact for token in ("是否", "是不是", "有没有", "还在吗", "了吗", "状态", "查一下")) or any(
        token in lowered for token in ("status", "still", "did you", "has it")
    )
    stateish = any(token in compact for token in ("取消", "停止", "暂停", "恢复", "追踪", "关注", "订阅", "任务"))
    return bool(questionish and stateish)


def _looks_like_direct_single_action(compact: str, lowered: str) -> bool:
    if _has_negated_action(compact):
        return False
    direct = any(token in compact for token in ("帮我取消", "取消", "停止", "暂停", "恢复", "帮我关注", "给我订阅", "每天推送")) or any(
        token in lowered for token in ("cancel", "stop", "pause", "resume", "subscribe", "track ")
    )
    shift = any(token in compact for token in ("换成", "改成", "转向", "不如", "没那么重要", "值得关注", "可能", "或许", "考虑"))
    return bool(direct and not shift)


def _has_negated_action(compact: str) -> bool:
    return any(token in compact for token in ("不要取消", "不用取消", "别取消", "先别取消", "不要停止", "不用停止", "先别订阅", "别订阅", "不要订阅"))


def _extract_shift_pair(text: str) -> tuple[str, str]:
    patterns = (
        rf"(?:取消|停止|停掉|不看|不再关注|别再关注)\s*(?P<old>{ENTITY_PATTERN}|[^，,。！？!?]{{2,24}}?)\s*(?:追踪|跟踪|关注|订阅|新闻|任务|计划)?[，,、\s]*(?:改成|换成|转向|改为)\s*(?P<new>{ENTITY_PATTERN}|[^，,。！？!?]{{2,24}})",
        rf"从\s*(?P<old>{ENTITY_PATTERN}|[^，,。！？!?]{{2,24}}?)\s*(?:转向|换成|迁到|改用|切到)\s*(?P<new>{ENTITY_PATTERN}|[^，,。！？!?]{{2,24}})",
        rf"(?P<old>{ENTITY_PATTERN})\s*(?:已经)?(?:不如|没有|没)\s*(?P<new>{ENTITY_PATTERN})",
        rf"用\s*(?P<old>{ENTITY_PATTERN})\s*(?:已经)?(?:不如|没有|没)\s*(?P<new>{ENTITY_PATTERN})",
        rf"(?P<old>{ENTITY_PATTERN})\s*(?:换成|改成|转向|改用|切到|改为)\s*(?P<new>{ENTITY_PATTERN})",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if not match:
            continue
        old = _clean_entity(match.group("old"))
        new = _clean_entity(match.group("new"))
        if old and new and old != new:
            return old, new
    return "", ""


def _extract_tentative_learning_topic(text: str, lowered: str) -> str:
    if not _mentions_learning(text, lowered):
        return ""
    if not any(token in text for token in ("可能", "或许", "也许", "考虑", "感觉", "似乎")) and not any(
        token in lowered for token in ("maybe", "might", "consider")
    ):
        return ""
    patterns = (
        rf"(?:学习|开始学|开始学习|学)\s*(?P<topic>{ENTITY_PATTERN})",
        r"(?:学习|开始学|开始学习|学)\s*(?P<topic>[^，,。！？!?]{2,32})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_entity(match.group("topic"))
    return ""


def _extract_decreased_topic(text: str, lowered: str) -> str:
    patterns = (
        rf"(?:不想继续|不太想继续|可能不想继续|不准备继续|不想再|不用继续)\s*(?:关注|追踪|跟踪|看)?\s*(?P<topic>{ENTITY_PATTERN})",
        rf"(?P<topic>{ENTITY_PATTERN}).{{0,12}}(?:没那么重要|不重要|不太适合|不合适|没必要|不用作为主线)",
        r"(?P<topic>[^，,。！？!?]{2,32}).{0,12}(?:没那么重要|不重要|不太适合|不合适|没必要|不用作为主线)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_entity(match.group("topic"))
    return ""


def _extract_preferred_topic(text: str, lowered: str) -> str:
    if not any(token in text for token in ("更适合", "更合适", "更重要", "更优先")) and not any(
        token in lowered for token in ("better fit", "more suitable", "prefer")
    ):
        return ""
    patterns = (
        rf"(?P<topic>{ENTITY_PATTERN}).{{0,8}}(?:可能)?(?:更适合|更合适|更重要|更优先)",
        r"(?P<topic>[^，,。！？!?]{2,32}).{0,8}(?:可能)?(?:更适合|更合适|更重要|更优先)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_entity(match.group("topic"))
    return ""


def _extract_attention_topic(text: str, lowered: str) -> str:
    patterns = (
        rf"(?P<topic>{ENTITY_PATTERN}).{{0,8}}(?:值得关注|可以关注|要留意|更重要|很重要)",
        r"(?P<topic>[^，,。！？!?]{2,32}).{0,8}(?:值得关注|可以关注|要留意|更重要|很重要)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_entity(match.group("topic"))
    return ""


def _extract_add_topic(text: str, lowered: str) -> str:
    patterns = (
        rf"(?:先加一个|加一个|新增|加上|加入)\s*(?P<topic>{ENTITY_PATTERN})",
        r"(?:先加一个|加一个|新增|加上|加入)\s*(?P<topic>[^，,。！？!?]{2,32})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_entity(match.group("topic"))
    return ""


def _extract_keep_topic(text: str, lowered: str) -> str:
    patterns = (
        rf"(?:不要取消|不用取消|别取消|不要停止|不用停止)\s*(?P<topic>{ENTITY_PATTERN})",
        r"(?P<topic>[^，,。！？!?]{2,32})\s*(?:不用取消|不要取消|别取消|不用停止|不要停止)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_entity(match.group("topic"))
    return ""


def _extract_hold_subscription_topic(text: str, lowered: str) -> str:
    patterns = (
        rf"(?:先别订阅|别订阅|不要订阅|先别关注|先别追踪)\s*(?P<topic>{ENTITY_PATTERN})",
        r"(?P<topic>[^，,。！？!?]{2,32})\s*(?:也)?(?:先别订阅|别订阅|不要订阅|先别关注|先别追踪)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_entity(match.group("topic"))
    return ""


def _mentions_learning(text: str, lowered: str) -> bool:
    return any(token in text for token in ("学习", "学", "入门", "教程", "课程")) or any(token in lowered for token in ("learn", "study"))


def _modality(text: str, lowered: str) -> str:
    if _looks_like_state_query(_compact(text), lowered):
        return "question"
    if any(token in text for token in ("可能", "或许", "也许", "考虑", "感觉", "似乎")) or any(token in lowered for token in ("maybe", "might")):
        return "tentative"
    if any(token in text for token in ("如果", "假如")) or "if " in lowered:
        return "hypothetical"
    if _has_negated_action(_compact(text)):
        return "negative"
    return "certain"


def _commitment_strength(text: str, lowered: str, proposed: list[ProposedAction]) -> float:
    if any(token in text for token in ("可能", "或许", "也许", "感觉", "似乎", "没那么", "值得关注")):
        return 0.55
    if any(token in text for token in ("考虑", "准备", "想开始", "我想")):
        return 0.72
    if proposed and any(token in text for token in ("帮我", "给我", "请", "以后", "换成", "加一个")):
        return 0.82
    return 0.64 if proposed else 0.45


def _speech_act(*, changes: list[StateChange], proposed_actions: list[ProposedAction], modality: str) -> str:
    fields = {change.state_field for change in changes}
    if any(change.operation in {"keep", "hold"} for change in changes) and not proposed_actions:
        return "project_context_update"
    if "project_relevance" in fields and len(changes) >= 2:
        return "preference_shift"
    if "learning_interest" in fields:
        return "learning_intent"
    if "attention_interest" in fields or "tracking_interest" in fields:
        return "interest_signal" if modality == "tentative" else "project_context_update"
    return "preference_change"


def _matching_commitment(commitments: Sequence[dict[str, Any]], topic: str, *, kinds: set[str]) -> dict[str, Any] | None:
    needle = _normalize_match(topic)
    if not needle:
        return None
    for item in reversed(list(commitments)):
        if not isinstance(item, dict) or str(item.get("kind") or "") not in kinds:
            continue
        if str(item.get("status") or "") not in {"active", "pending_confirmation", "paused"}:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        haystack = _normalize_match(" ".join(str(part or "") for part in (item.get("title"), payload.get("topic"), payload.get("query"))))
        if needle and (needle in haystack or haystack in needle):
            return item
    return None


def _dedupe_actions(actions: list[ProposedAction]) -> list[ProposedAction]:
    seen: set[tuple[str, str]] = set()
    result: list[ProposedAction] = []
    for action in actions:
        key = (action.action, _normalize_match(action.target))
        if key in seen:
            continue
        seen.add(key)
        result.append(action)
    return result


def _dedupe_entities(entities: list[EntityMention]) -> list[EntityMention]:
    seen: set[tuple[str, str]] = set()
    result: list[EntityMention] = []
    for entity in entities:
        key = (_normalize_match(entity.name), entity.role)
        if key in seen:
            continue
        seen.add(key)
        result.append(entity)
    return result


def _clean_entity(value: str) -> str:
    text = str(value or "").strip(" 的了吧吗呢啊？?！!，,。")
    text = re.sub(r"^(?:接下来|以后|今后|后面|未来)?(?:我的|我这个)?(?:项目|主线|技术栈)?(?:用|从)?\s*", "", text)
    text = re.sub(r"(?:也?先)$", "", text).strip(" 的了吧吗呢啊？?！!，,。")
    text = re.sub(r"(?:了|吧|吗|呢|啊|相关内容|资料|课程|追踪|关注|订阅)$", "", text).strip(" 的了吧吗呢啊？?！!，,。")
    if not text or text in {"这个", "那个", "一个", "相关", "项目", "技术栈", "学习", "追踪", "关注", "订阅"}:
        return ""
    if len(text) > 40:
        return ""
    return re.sub(r"\s+", " ", text)


def _normalize_match(value: str) -> str:
    return re.sub(r"[\s，,。！？!?、·:：；;（）()【】\[\]_\-]+", "", str(value or "").lower())


def _compact(value: str) -> str:
    return re.sub(r"[\s，,。！？!?、]+", "", value or "")


def _field_label(value: str) -> str:
    return {
        "project_relevance": "项目相关性",
        "learning_interest": "学习兴趣",
        "attention_interest": "关注兴趣",
        "tracking_interest": "追踪意向",
        "tracking_status": "追踪状态",
    }.get(value, value)


def _operation_label(operation: str, direction: str, new_value: str) -> str:
    if operation == "increase":
        return "上升"
    if operation == "decrease":
        return "下降"
    if operation == "keep":
        return "保持不变"
    if operation == "hold":
        return "暂不开启"
    if new_value:
        return f"设为 {new_value}"
    return f" {operation}".rstrip()


def _action_label(action: dict[str, Any]) -> str:
    target = str(action.get("target") or "这个主题")
    kind = str(action.get("action") or "")
    return {
        "deprioritize_tracking": f"降级/暂停 {target} 的现有追踪",
        "create_learning_tracking": f"新增 {target} 学习追踪",
        "create_tracking": f"新增 {target} 外部追踪",
    }.get(kind, f"{target}：{kind}")
