from __future__ import annotations

import copy
import math
from datetime import datetime, timezone
from typing import Any, Iterable

from core.context_scope import tenant_scope_storage_key
from core.semantic_frame import (
    TurnSemanticFrame,
    semantic_frame_quality_issues,
)
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, utc_now_iso
from memory_bridge.scope import framed_sha256, normalize_scope_component


class AttentionCore:
    """Owner-scoped turn focus with a fail-closed semantic refinement step.

    This component is deliberately not the proactive Attention scheduler.  Its
    output is a compact retrieval/routing hint for one foreground turn.  Initial
    focus may only come from exact structured event references or an exact
    owner/session continuation.  Free-text topic keywords never create focus.

    A validated model semantic frame may add focus after UnderstandingCore has
    run.  Model transport failure, malformed output, reported speech,
    hypothetical mentions, and unresolved referents cannot upgrade focus.
    Nothing in this class grants a capability or authorizes an effect.
    """

    STATE_FILE = "attention_state.json"
    SCHEMA_VERSION = "veyra.attention_state.v2"
    SCORER_VERSION = "veyra.turn_focus.semantic.v1"
    MAX_SCOPES = 200
    MAX_FOCUS = 12
    MIN_SEMANTIC_SCORE = 0.75
    _DIRECT_USER_SPEAKERS = frozenset(
        {"user", "current_user", "requester", "direct_user"}
    )

    _CONTINUATIONS = frozenset(
        {
            "继续",
            "接着",
            "继续说",
            "继续处理",
            "继续刚才",
            "继续上次",
            "继续昨天那个项目",
            "continue",
            "resume previous",
            "same topic",
        }
    )
    _DIRECT_REF_FIELDS = {
        "goal_id": "goal",
        "commitment_id": "commitment",
        "case_id": "case",
        "task_id": "task",
        "trace_id": "trace",
        "workspace_id": "workspace",
        "project_id": "project",
        "entity_id": "entity",
    }
    _REF_COLLECTIONS = {
        "goal_refs": "goal",
        "commitment_refs": "commitment",
        "case_refs": "case",
        "task_refs": "task",
        "trace_refs": "trace",
        "entity_refs": "entity",
        "evidence_refs": "evidence",
        "structured_focus_refs": "structured",
    }

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def focus_for_text(
        self,
        text: str,
        *,
        user_id: str,
        session_id: str,
        event: VeyraEvent | None = None,
    ) -> list[str]:
        """Create the pre-understanding focus for one exact owner scope.

        ``text`` is used only to recognize a small exact continuation command;
        it is never scanned for a subject, project, tool, or domain keyword.
        """

        owner = self._owner(user_id, session_id)
        if owner is None or not self._event_matches_owner(event, owner):
            return []
        selected_user, selected_session, scope_key = owner
        state, previous_record = self._scope_record(scope_key, owner)
        if self._scope_expired(previous_record):
            previous_record = {}
        previous_focus = self._focus_list(previous_record.get("focus"))
        structured = self._structured_event_assessments(event)
        inherited = False
        if self._is_continuation(text):
            inherited = bool(previous_focus)
            if inherited:
                structured = self._merge_assessments(
                    structured,
                    self._continuation_assessments(previous_record),
                )
        focus = self._ranked_focus(structured)
        record = {
            "user_id": selected_user,
            "session_id": selected_session,
            "focus": focus,
            "component_scores": structured,
            "ignored_noise": ["free_text_topic_mapping_disabled"],
            "context_scope": self._context_scope(focus),
            "previous_focus": previous_focus,
            "inherited_from_previous": inherited,
            "stage": "initial",
            "source": (
                "structured_event_refs"
                if any(
                    str(item.get("source") or "") == "structured_event_ref"
                    for item in structured
                )
                else "owner_scoped_continuation"
                if inherited
                else "no_structured_focus"
            ),
            "refinement": {
                "status": "not_started",
                "scorer_version": self.SCORER_VERSION,
                "model_validated": False,
            },
            "confidence": self._confidence(structured),
            "ttl_seconds": 300,
            "turn_binding": self._turn_binding(text, owner, event),
            "updated_at": utc_now_iso(),
        }
        if state.get("_state_corrupt") is not True:
            self._persist_scope(scope_key, record)
        return focus

    def assess_understanding(
        self,
        *,
        text: str,
        understanding: Any,
    ) -> dict[str, Any]:
        """Purely assess a validated semantic frame without changing state."""

        source = str(getattr(understanding, "source", "") or "")
        if source not in {"model", "model_repair"}:
            return self._rejected_assessment("understanding_source_not_validated_model")
        frame_value = getattr(understanding, "semantic_frame", None)
        if frame_value is None:
            return self._rejected_assessment("semantic_frame_missing")
        try:
            frame_payload = (
                frame_value.model_dump(mode="json")
                if hasattr(frame_value, "model_dump")
                else copy.deepcopy(frame_value)
            )
            if not isinstance(frame_payload, dict):
                raise TypeError("semantic frame must be an object")
            frame = TurnSemanticFrame.from_model_payload(
                frame_payload,
                source_text=str(text or ""),
            )
            quality_issues = semantic_frame_quality_issues(
                frame,
                str(text or ""),
            )
        except (TypeError, ValueError) as exc:
            return self._rejected_assessment(
                "semantic_frame_validation_failed",
                detail=type(exc).__name__,
            )
        if quality_issues:
            return {
                **self._rejected_assessment("semantic_frame_quality_failed"),
                "quality_issues": quality_issues[:8],
            }

        ambiguous_acts = {
            str(act_id)
            for ambiguity in frame.ambiguities
            for act_id in ambiguity.affected_act_ids
        }
        rows: list[dict[str, Any]] = []
        for act in frame.acts:
            row = self._semantic_act_assessment(
                act,
                ambiguous=act.act_id in ambiguous_acts,
            )
            if row is not None:
                rows.append(row)
        rows = self._merge_assessments([], rows)
        eligible = [
            row
            for row in rows
            if row.get("eligible") is True
            and float(row.get("score") or 0.0) >= self.MIN_SEMANTIC_SCORE
        ]
        blockers = list(
            dict.fromkeys(
                str(blocker)
                for row in rows
                if isinstance(row, dict) and row.get("eligible") is not True
                for blocker in (
                    row.get("blockers")
                    if isinstance(row.get("blockers"), list)
                    else []
                )
                if str(blocker)
            )
        )
        return {
            "status": "assessed",
            "scorer_version": self.SCORER_VERSION,
            "model_validated": True,
            "semantic_frame_source": frame.source,
            "resolver_status": frame.resolver_status,
            "focus": self._ranked_focus(eligible),
            "component_scores": rows,
            "blockers": blockers,
        }

    def refine_from_understanding(
        self,
        *,
        text: str,
        understanding: Any,
        user_id: str,
        session_id: str,
        event: VeyraEvent | None = None,
    ) -> dict[str, Any]:
        """Add validated semantic focus to the current exact-owner turn."""

        owner = self._owner(user_id, session_id)
        if owner is None:
            return {
                "status": "not_upgraded",
                "focus": [],
                "reason": "owner_scope_invalid",
            }
        if not self._event_matches_owner(event, owner):
            return {
                "status": "not_upgraded",
                "focus": [],
                "reason": "event_owner_scope_mismatch",
            }
        selected_user, selected_session, scope_key = owner
        state, current = self._scope_record(scope_key, owner)
        current_focus = self._focus_list(current.get("focus"))
        if str(current.get("turn_binding") or "") != self._turn_binding(
            text,
            owner,
            event,
        ):
            return {
                "status": "not_upgraded",
                "focus": current_focus,
                "reason": "attention_turn_binding_missing_or_mismatch",
            }
        assessment = self.assess_understanding(
            text=text,
            understanding=understanding,
        )
        if state.get("_state_corrupt") is True:
            return {
                "status": "not_upgraded",
                "focus": current_focus,
                "reason": "attention_state_corrupt",
                "assessment": assessment,
            }

        existing_rows = (
            copy.deepcopy(current.get("component_scores"))
            if isinstance(current.get("component_scores"), list)
            else []
        )
        if assessment.get("status") == "assessed":
            semantic_rows = [
                copy.deepcopy(item)
                for item in assessment.get("component_scores", [])
                if isinstance(item, dict) and item.get("eligible") is True
            ]
            if semantic_rows:
                merged_rows = self._merge_assessments(existing_rows, semantic_rows)
                focus = self._ranked_focus(merged_rows)
                status = "refined"
                source = "validated_semantic_frame"
            else:
                merged_rows = existing_rows
                focus = current_focus
                status = "not_upgraded"
                source = str(current.get("source") or "no_structured_focus")
        else:
            merged_rows = existing_rows
            focus = current_focus
            status = "not_upgraded"
            source = str(current.get("source") or "no_structured_focus")

        record = {
            **copy.deepcopy(current),
            "user_id": selected_user,
            "session_id": selected_session,
            "focus": focus,
            "component_scores": merged_rows,
            "context_scope": self._context_scope(focus),
            "stage": "refined" if status == "refined" else "initial",
            "source": source,
            "refinement": {
                "status": status,
                "scorer_version": self.SCORER_VERSION,
                "model_validated": assessment.get("model_validated") is True,
                "reason": (
                    None
                    if status == "refined"
                    else str(assessment.get("reason") or "semantic_focus_unavailable")
                ),
                "resolver_status": assessment.get("resolver_status"),
                "semantic_frame_source": assessment.get("semantic_frame_source"),
                "blockers": copy.deepcopy(assessment.get("blockers") or []),
            },
            "confidence": self._confidence(merged_rows),
            "ttl_seconds": 300,
            "updated_at": utc_now_iso(),
        }
        self._persist_scope(scope_key, record)
        return {
            "status": status,
            "focus": focus,
            "source": source,
            "confidence": record["confidence"],
            "reason": record["refinement"].get("reason"),
            "assessment": assessment,
        }

    def active_scope(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        owner = self._owner(user_id, session_id)
        if owner is None:
            raise ValueError("user_id and session_id must form a valid exact scope")
        _, _, scope_key = owner
        state, record = self._scope_record(scope_key, owner)
        if state.get("_state_corrupt") is True:
            return {
                "status": "degraded",
                "scope_status": "state_corrupt",
                "source": "attention_core",
                "focus": [],
                "context_scope": self._context_scope([]),
                "ignored_noise": [],
            }
        if self._scope_expired(record):
            return {
                "status": "success",
                "scope_status": "expired",
                "source": record.get("source") or "attention_core",
                "updated_at": record.get("updated_at"),
                "confidence": 0.0,
                "ttl_seconds": record.get("ttl_seconds", 300),
                "focus": [],
                "context_scope": self._context_scope([]),
                "ignored_noise": ["scope_ttl_expired"],
                "previous_focus": [],
                "inherited_from_previous": False,
                "stage": record.get("stage"),
                "refinement": {},
                "component_scores": [],
            }
        focus = self._focus_list(record.get("focus"))
        return {
            "status": "success",
            "scope_status": "exact" if record else "not_found",
            "source": record.get("source") or "attention_core",
            "updated_at": record.get("updated_at"),
            "confidence": record.get("confidence", 0.0),
            "ttl_seconds": record.get("ttl_seconds", 300),
            "focus": focus,
            "context_scope": (
                copy.deepcopy(record.get("context_scope"))
                if isinstance(record.get("context_scope"), dict)
                else self._context_scope(focus)
            ),
            "ignored_noise": (
                copy.deepcopy(record.get("ignored_noise"))
                if isinstance(record.get("ignored_noise"), list)
                else []
            ),
            "previous_focus": self._focus_list(record.get("previous_focus")),
            "inherited_from_previous": bool(
                record.get("inherited_from_previous")
            ),
            "stage": record.get("stage"),
            "refinement": (
                copy.deepcopy(record.get("refinement"))
                if isinstance(record.get("refinement"), dict)
                else {}
            ),
            "component_scores": (
                copy.deepcopy(record.get("component_scores"))
                if isinstance(record.get("component_scores"), list)
                else []
            ),
        }

    def _semantic_act_assessment(
        self,
        act: Any,
        *,
        ambiguous: bool,
    ) -> dict[str, Any] | None:
        target_type = self._focus_component(getattr(act.target, "type", ""), 80)
        target_value = self._focus_component(getattr(act.target, "value", ""), 180)
        referent_status = str(getattr(act.referent, "status", "") or "")
        if not target_value and referent_status == "resolved":
            target_value = self._focus_component(
                getattr(act.referent, "resolved", ""),
                180,
            )
        if not target_type or target_type == "unknown" or not target_value:
            return None

        authority = str(getattr(act, "authority", "") or "")
        speaker = str(getattr(act, "speaker", "") or "")
        mention_mode = str(getattr(act, "mention_mode", "") or "")
        modality = str(getattr(act, "modality", "") or "")
        explicitness = str(getattr(act, "explicitness", "") or "")
        # Some structured-output providers use ``direct_user`` as both the
        # authority token and the actor token.  This is a schema vocabulary
        # alias, not a natural-language inference: reported/quoted speakers
        # remain ineligible and this score still grants no capability.
        direct_user = (
            authority == "direct_user"
            and speaker in self._DIRECT_USER_SPEAKERS
        )
        normal_use = mention_mode == "normal_use"
        non_hypothetical = modality not in {
            "hypothetical",
            "reported",
            "quoted",
        }
        resolved = referent_status in {"resolved", "not_applicable"}
        contributions = {
            "validated_model_contract": 0.25,
            "exact_source_quote_binding": 0.20,
            "direct_user_authority": 0.20 if direct_user else 0.0,
            "normal_mention": 0.10 if normal_use else 0.0,
            "explicitness": {
                "explicit": 0.15,
                "strong_implied": 0.10,
                "weak_implied": 0.04,
                "inferred": 0.0,
                "unknown": 0.0,
            }.get(explicitness, 0.0),
            "referent_resolution": 0.05 if resolved else 0.0,
            "specific_target": 0.05,
        }
        score = round(sum(contributions.values()), 6)
        blockers: list[str] = []
        if not direct_user:
            blockers.append("not_direct_user_speaker_authority")
        if not normal_use:
            blockers.append("non_normal_mention")
        if not non_hypothetical:
            blockers.append("hypothetical_or_reported_modality")
        if not resolved:
            blockers.append("referent_unresolved")
        if ambiguous:
            blockers.append("semantic_act_ambiguous")
        eligible = not blockers and score >= self.MIN_SEMANTIC_SCORE
        focus = f"target:{target_type}:{target_value}"
        return {
            "focus": focus,
            "score": score,
            "eligible": eligible,
            "source": "validated_semantic_frame",
            "act_ids": [str(getattr(act, "act_id", ""))],
            "components": contributions,
            "blockers": blockers,
        }

    def _structured_event_assessments(
        self,
        event: VeyraEvent | None,
    ) -> list[dict[str, Any]]:
        if event is None:
            return []
        refs: list[tuple[str, str]] = []
        for subject in self._records(event.subject):
            kind = self._focus_component(
                subject.get("kind") or subject.get("type"),
                80,
            )
            ref_id = self._focus_component(
                subject.get("ref_id")
                or subject.get("id")
                or subject.get("value"),
                180,
            )
            if kind and ref_id:
                refs.append((kind, ref_id))
            refs.extend(self._direct_refs(subject))
        payload = event.payload if isinstance(event.payload, dict) else {}
        refs.extend(self._direct_refs(payload))
        for key, kind in self._REF_COLLECTIONS.items():
            refs.extend(self._refs(payload.get(key), default_kind=kind))
        refs.extend(self._refs(event.evidence_refs, default_kind="evidence"))

        unique: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for kind, ref_id in refs:
            identity = (kind, ref_id)
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(identity)
        return [
            {
                "focus": f"ref:{kind}:{ref_id}",
                "score": 1.0,
                "eligible": True,
                "source": "structured_event_ref",
                "components": {
                    "exact_owner_scope": 0.5,
                    "structured_event_reference": 0.5,
                },
                "blockers": [],
            }
            for kind, ref_id in unique[: self.MAX_FOCUS]
        ]

    def _direct_refs(self, value: Any) -> list[tuple[str, str]]:
        if not isinstance(value, dict):
            return []
        refs: list[tuple[str, str]] = []
        for field, kind in self._DIRECT_REF_FIELDS.items():
            ref_id = self._focus_component(value.get(field), 180)
            if ref_id:
                refs.append((kind, ref_id))
        return refs

    def _refs(self, value: Any, *, default_kind: str) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                kind = self._focus_component(
                    item.get("kind") or item.get("type") or default_kind,
                    80,
                )
                ref_id = self._focus_component(
                    item.get("ref_id") or item.get("id") or item.get("value"),
                    180,
                )
            else:
                kind = default_kind
                ref_id = self._focus_component(item, 180)
            if kind and ref_id:
                refs.append((kind, ref_id))
        return refs

    def _scope_record(
        self,
        scope_key: str,
        owner: tuple[str, str, str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self.state_store.read_json(self.STATE_FILE)
        if state.get("_state_corrupt") is True:
            return state, {}
        scopes = state.get("scopes") if isinstance(state.get("scopes"), dict) else {}
        record = scopes.get(scope_key) if isinstance(scopes.get(scope_key), dict) else {}
        if not record:
            # Legacy v1 top-level focus is deliberately not inherited.  It had
            # no owner binding and therefore cannot be assigned safely.
            return state, {}
        if (
            str(record.get("user_id") or "") != owner[0]
            or str(record.get("session_id") or "") != owner[1]
        ):
            return state, {}
        return state, copy.deepcopy(record)

    @staticmethod
    def _scope_expired(record: dict[str, Any]) -> bool:
        """Return freshness without mutating or pruning the persisted scope."""

        if not record:
            return False
        raw_ttl = record.get("ttl_seconds", 300)
        if isinstance(raw_ttl, bool):
            return True
        try:
            ttl_seconds = float(raw_ttl)
        except (TypeError, ValueError):
            return True
        if not math.isfinite(ttl_seconds):
            return True
        if ttl_seconds <= 0:
            return False

        updated_at = str(record.get("updated_at") or "")
        if not updated_at:
            return True
        try:
            parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - parsed).total_seconds() >= ttl_seconds

    def _persist_scope(self, scope_key: str, record: dict[str, Any]) -> bool:
        persisted = False

        def update(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal persisted
            if state.get("_state_corrupt") is True:
                return state
            scopes = state.get("scopes") if isinstance(state.get("scopes"), dict) else {}
            scopes = copy.deepcopy(scopes)
            scopes[scope_key] = copy.deepcopy(record)
            ordered = sorted(
                scopes.items(),
                key=lambda item: (
                    str(item[1].get("updated_at") or "")
                    if isinstance(item[1], dict)
                    else "",
                    str(item[0]),
                ),
            )[-self.MAX_SCOPES :]
            state["schema_version"] = self.SCHEMA_VERSION
            state["source"] = "attention_core"
            state["scopes"] = dict(ordered)
            state["scope_count"] = len(ordered)
            # Remove the unscoped v1 projection on the first safe v2 write.
            for key in (
                "focus",
                "previous_focus",
                "context_scope",
                "ignored_noise",
                "inherited_from_previous",
                "stage",
                "refinement",
            ):
                state.pop(key, None)
            persisted = True
            return state

        self.state_store.mutate_json(self.STATE_FILE, update)
        return persisted

    def _continuation_assessments(
        self,
        previous_record: dict[str, Any],
    ) -> list[dict[str, Any]]:
        prior_rows = (
            previous_record.get("component_scores")
            if isinstance(previous_record.get("component_scores"), list)
            else []
        )
        by_focus = {
            str(item.get("focus") or ""): copy.deepcopy(item)
            for item in prior_rows
            if isinstance(item, dict) and str(item.get("focus") or "")
        }
        output: list[dict[str, Any]] = []
        for focus in self._focus_list(previous_record.get("focus")):
            row = by_focus.get(focus) or {
                "focus": focus,
                "score": float(previous_record.get("confidence") or 0.0),
                "eligible": True,
                "components": {},
                "blockers": [],
            }
            row["source"] = "owner_scoped_continuation"
            row["inherited"] = True
            output.append(row)
        return output

    def _merge_assessments(
        self,
        left: Iterable[dict[str, Any]],
        right: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        selected: dict[str, dict[str, Any]] = {}
        for raw in [*left, *right]:
            if not isinstance(raw, dict):
                continue
            focus = str(raw.get("focus") or "")
            if not focus:
                continue
            current = selected.get(focus)
            if current is None or float(raw.get("score") or 0.0) > float(
                current.get("score") or 0.0
            ):
                selected[focus] = copy.deepcopy(raw)
                continue
            if isinstance(raw.get("act_ids"), list):
                current_ids = (
                    current.get("act_ids")
                    if isinstance(current.get("act_ids"), list)
                    else []
                )
                current["act_ids"] = list(
                    dict.fromkeys(
                        [str(item) for item in [*current_ids, *raw["act_ids"]] if str(item)]
                    )
                )[:8]
        return sorted(
            selected.values(),
            key=lambda item: (-float(item.get("score") or 0.0), str(item.get("focus") or "")),
        )[: self.MAX_FOCUS]

    def _ranked_focus(self, rows: Iterable[dict[str, Any]]) -> list[str]:
        selected = [
            copy.deepcopy(item)
            for item in rows
            if isinstance(item, dict)
            and item.get("eligible") is True
            and str(item.get("focus") or "")
        ]
        selected.sort(
            key=lambda item: (-float(item.get("score") or 0.0), str(item.get("focus") or ""))
        )
        return list(
            dict.fromkeys(str(item["focus"]) for item in selected)
        )[: self.MAX_FOCUS]

    def _owner(self, user_id: Any, session_id: Any) -> tuple[str, str, str] | None:
        try:
            selected_user = normalize_scope_component(user_id, "user_id")
            selected_session = normalize_scope_component(session_id, "session_id")
            scope_key = tenant_scope_storage_key(selected_user, selected_session)
        except ValueError:
            return None
        return selected_user, selected_session, scope_key

    def _event_matches_owner(
        self,
        event: VeyraEvent | None,
        owner: tuple[str, str, str],
    ) -> bool:
        if event is None:
            return True
        try:
            event_user = normalize_scope_component(
                event.source.user_id,
                "user_id",
            )
            event_session = normalize_scope_component(
                event.source.session_id,
                "session_id",
            )
        except ValueError:
            return False
        return event_user == owner[0] and event_session == owner[1]

    def _context_scope(self, focus: list[str]) -> dict[str, list[str]]:
        structured_refs = [item for item in focus if item.startswith("ref:")]
        semantic_targets = [item for item in focus if item.startswith("target:")]
        return {
            "probe_priority": [],
            "structured_refs": structured_refs[: self.MAX_FOCUS],
            "semantic_targets": semantic_targets[: self.MAX_FOCUS],
        }

    @staticmethod
    def _turn_binding(
        text: str,
        owner: tuple[str, str, str],
        event: VeyraEvent | None,
    ) -> str:
        return framed_sha256(
            "veyra-attention-turn-v1",
            owner[0],
            owner[1],
            str(getattr(event, "event_id", "") or ""),
            str(text or ""),
        )

    def _is_continuation(self, text: str) -> bool:
        compact = " ".join(str(text or "").strip().split()).strip("，,。！？!?")
        return compact.casefold() in self._CONTINUATIONS

    @staticmethod
    def _focus_component(value: Any, limit: int) -> str:
        if isinstance(value, bool) or value is None:
            return ""
        if not isinstance(value, (str, int, float)):
            return ""
        if isinstance(value, float) and not math.isfinite(value):
            return ""
        selected = " ".join(str(value).strip().split())
        if not selected or any(ord(character) < 32 for character in selected):
            return ""
        return selected.replace(":", "_")[:limit]

    @staticmethod
    def _records(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _focus_list(value: Any) -> list[str]:
        return [str(item) for item in value if str(item)] if isinstance(value, list) else []

    @staticmethod
    def _confidence(rows: Iterable[dict[str, Any]]) -> float:
        scores = [
            float(item.get("score") or 0.0)
            for item in rows
            if isinstance(item, dict) and item.get("eligible") is True
        ]
        return round(max(scores), 6) if scores else 0.0

    def _rejected_assessment(
        self,
        reason: str,
        *,
        detail: str | None = None,
    ) -> dict[str, Any]:
        blockers = [reason]
        if detail:
            blockers.append(detail)
        return {
            "status": "not_assessed",
            "scorer_version": self.SCORER_VERSION,
            "model_validated": False,
            "focus": [],
            "component_scores": [],
            "reason": reason,
            "blockers": blockers,
        }
