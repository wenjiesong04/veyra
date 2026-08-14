"""Small, read-only product projection for the local Veyra preview.

The product surface deliberately sits on top of the existing Goal, Situation,
Attention and Suggestion stores.  It does not create a second source of truth
and it never widens an authority boundary.  Every projection is resolved for
one exact owner/session; ambiguous workspace Goals and session mismatches are
returned as explicit setup states instead of being guessed through.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import os
from typing import Any, Callable

from core.context_scope import owner_scope
from core.goal_store_policy import (
    WORKSPACE_GOAL_KIND,
    WORKSPACE_GOAL_SCHEMA,
    WORKSPACE_GOAL_SOURCE,
)
from core.world_state import WorldStateStore
from interface.session_mapper import SessionMapper
from memory_bridge.scope import normalize_scope_component
from runtime.attention_hypothesis_runtime import AttentionHypothesisRuntime
from runtime.general_situation_runtime import GeneralSituationRuntime
from runtime.product_experience_projection import (
    blocked_today_projection,
    build_projection,
    cognition_projection,
    configuration_label,
    readiness_projection,
    matters_status,
    section_status,
    section_projection,
)
from runtime.suggestion_outbox import SuggestionOutbox

PRODUCT_CONTEXT_SCHEMA = "veyra.product_context.v1"
PRODUCT_TODAY_SCHEMA = "veyra.product_today.v1"
PRODUCT_MATTERS_SCHEMA = "veyra.product_matters.v1"
PRODUCT_STATUS_SCHEMA = "veyra.product_status.v1"
PRODUCT_CHANNEL = "api"
DEFAULT_EXTERNAL_SESSION = "veyra-workspace-primary-v1"
LOCAL_EXTERNAL_SESSION = "veyra-product-local-v1"


def _authority() -> dict[str, bool]:
    # Product GETs can only expose existing read models.  Keep all existing
    # execution, delivery, route and grant boundaries visibly disabled.
    return {
        "execution_allowed": False,
        "tool_allowed": False,
        "agent_allowed": False,
        "capability_grant_allowed": False,
        "route_change_allowed": False,
        "external_delivery_allowed": False,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _freshness(value: dict[str, Any], *, now: datetime | None = None) -> str:
    """Project state freshness without exposing the underlying state path."""

    current = now or _now()
    updated = _time(value.get("updated_at"))
    if updated is None:
        return "unknown"
    ttl = value.get("ttl_seconds")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        return "fresh"
    # Runtime projections use ``ttl_seconds=0`` to mean that the producer is
    # authoritative and no age-based expiry is applied.  Do not call those
    # states stale merely because a GET happens later.
    if float(ttl) <= 0:
        return "fresh"
    return "fresh" if (current - updated).total_seconds() <= float(ttl) else "stale"


def _text(value: Any, fallback: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    return fallback


def _list_text(value: Any, *, maximum: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:240])
        elif isinstance(item, dict):
            # Do not surface evidence IDs/digests.  Keep only a human label
            # where an upstream producer supplied one.
            label = _text(item.get("label") or item.get("name") or item.get("kind"))
            if label:
                result.append(label[:240])
        if len(result) >= maximum:
            break
    return result


class ProductExperienceService:
    """Server-owned read models for the local-first Product Preview."""

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        session_mapper: SessionMapper | None = None,
        channel: str = PRODUCT_CHANNEL,
        external_session_id: str | None = None,
        runtime_build_resolver: Callable[[], dict[str, Any]] | None = None,
        agent_status_resolver: Callable[[], dict[str, Any]] | None = None,
        integration_status_resolver: Callable[[], dict[str, Any]] | None = None,
        cognition_status_resolver: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.state_store = state_store
        self.session_mapper = session_mapper or SessionMapper()
        self.channel = normalize_scope_component(channel, "channel")
        configured_external = external_session_id or os.getenv(
            "VEYRA_PRODUCT_EXTERNAL_SESSION_ID"
        )
        self.external_session_id = _text(configured_external, DEFAULT_EXTERNAL_SESSION)
        self.runtime_build_resolver = runtime_build_resolver
        self.agent_status_resolver = agent_status_resolver
        self.integration_status_resolver = integration_status_resolver
        self.cognition_status_resolver = cognition_status_resolver
        self.general_situations = GeneralSituationRuntime(state_store)
        self.attention_hypotheses = AttentionHypothesisRuntime(state_store)
        self.suggestion_outbox = SuggestionOutbox(state_store)

    # ------------------------------------------------------------------
    # Context and exact scope
    # ------------------------------------------------------------------
    def context(
        self,
        *,
        user_id: str | None = None,
        external_session_id: str | None = None,
    ) -> dict[str, Any]:
        requested_user = self._optional_scope(user_id, "user_id")
        goals = self._active_workspace_goals()
        if goals is None:
            return self._context_error("goal_state_unavailable")
        if requested_user is None:
            owners = sorted({str(item.get("user_id") or "") for item in goals})
            owners = [value for value in owners if value]
            if len(owners) > 1:
                return self._context_error("ambiguous_active_workspace_goals", status="ambiguous")
            selected_user = owners[0] if owners else "local-user"
        else:
            selected_user = requested_user

        matching = [
            item for item in goals if str(item.get("user_id") or "") == selected_user
        ]
        if len(matching) > 1:
            return self._context_error("ambiguous_active_workspace_goals", status="ambiguous")

        selected_external_session = external_session_id
        if selected_external_session is None:
            selected_external_session = self.external_session_id if matching else LOCAL_EXTERNAL_SESSION
        external_session = self._scope(selected_external_session, "external_session_id")

        mapped_session = self.session_mapper.map(
            self.channel,
            selected_user,
            external_session,
        )
        base = {
            "schema_version": PRODUCT_CONTEXT_SCHEMA,
            "channel": self.channel,
            "requested_external_session_id": external_session,
            "authority": _authority(),
        }
        if matching:
            goal = matching[0]
            goal_session = _text(goal.get("session_id"))
            if not goal_session or mapped_session != goal_session:
                return {
                    **base,
                    "status": "needs_session_link",
                    "reason": "external_session_does_not_match_active_workspace_goal",
                    "internal_read_scope": None,
                    "external_input_scope": {
                        "channel": self.channel,
                        "user_id": selected_user,
                        "session_id": external_session,
                    },
                    "goal": None,
                }
            return {
                **base,
                "status": "ready",
                "reason": None,
                "internal_read_scope": {
                    "user_id": selected_user,
                    "session_id": goal_session,
                },
                "external_input_scope": {
                    "channel": self.channel,
                    "user_id": selected_user,
                    "session_id": external_session,
                },
                "goal": self._goal_projection(goal),
                "freshness": self._source_freshness("user_goals.json"),
            }

        # A local user without a Goal gets an empty, server-defined scope.  It
        # is useful for a first conversation but never borrows another owner's
        # Goal or Situation records.
        return {
            **base,
            "status": "empty",
            "reason": "no_active_workspace_goal",
            "internal_read_scope": {
                "user_id": selected_user,
                "session_id": mapped_session,
            },
            "external_input_scope": {
                "channel": self.channel,
                "user_id": selected_user,
                "session_id": external_session,
            },
            "goal": None,
            "freshness": self._source_freshness("user_goals.json"),
        }


    def today(
        self,
        *,
        user_id: str,
        session_id: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        user, session = self._scope_pair(user_id, session_id)
        scope = {"user_id": user, "session_id": session}
        goals = self._active_workspace_goals()
        if goals is None:
            return {
                "schema_version": PRODUCT_TODAY_SCHEMA,
                "status": "fail_closed",
                "reason": "goal_state_unavailable",
                "scope": scope,
                "goal": None,
                "situations": [],
                "attention": [],
                "suggestions": [],
                "questions": {"status": "unsupported", "items": []},
                "waiting": [],
                "section_statuses": {
                    "situations": "fail_closed",
                    "attention": "fail_closed",
                    "suggestions": "fail_closed",
                    "questions": "unsupported",
                    "waiting": "fail_closed",
                },
                "freshness": self._projection_freshness(),
                "authority": _authority(),
            }
        matches = [
            item
            for item in goals
            if _text(item.get("user_id")) == user
            and _text(item.get("session_id")) == session
        ]
        owner_goals = [item for item in goals if _text(item.get("user_id")) == user]
        if len(owner_goals) > 1:
            return blocked_today_projection(
                scope,
                status="ambiguous",
                reason="ambiguous_active_workspace_goals",
                freshness=self._projection_freshness(),
                authority=_authority(),
            )
        if len(owner_goals) == 1 and not matches:
            return blocked_today_projection(
                scope,
                status="needs_session_link",
                reason="internal_session_does_not_match_active_workspace_goal",
                freshness=self._projection_freshness(),
                authority=_authority(),
            )
        if not owner_goals:
            return blocked_today_projection(
                scope,
                status="empty",
                reason="no_active_workspace_goal",
                freshness=self._projection_freshness(),
                authority=_authority(),
            )
        goal = owner_goals[0]

        situation_result = self.general_situations.list_for_owner(
            user_id=user,
            session_id=session,
            limit=max(1, min(int(limit), 100)),
        )
        attention_result = self.attention_hypotheses.list_for_owner(
            user_id=user,
            session_id=session,
            limit=max(1, min(int(limit), 100)),
        )
        preview_result = self.suggestion_outbox.list_preview(
            user_id=user,
            session_id=session,
            limit=max(1, min(int(limit), 100)),
        )
        decision_result = self.suggestion_outbox.list_decisions(
            user_id=user,
            session_id=session,
            limit=max(1, min(int(limit), 100)),
        )
        malformed_situations = any(
            isinstance(item, dict)
            and (
                "distinct_event_count" in item
                and (
                    isinstance(item.get("distinct_event_count"), bool)
                    or not isinstance(item.get("distinct_event_count"), int)
                )
            )
            for item in situation_result.get("items", [])
        )
        valid_situations = [
            item
            for item in situation_result.get("items", [])
            if isinstance(item, dict) and self._valid_situation(item)
        ]
        situations = [
            self._situation_projection(item, goal=goal)
            for item in valid_situations
            if self._current_item(item)
        ]
        situation_ids = {
            str(item.get("general_situation_id") or "")
            for item in valid_situations
            if self._current_item(item) and str(item.get("general_situation_id") or "")
        }
        attention = [
            self._attention_projection(item)
            for item in attention_result.get("items", [])
            if isinstance(item, dict)
            and str(item.get("general_situation_id") or "") in situation_ids
            and self._current_item(item)
        ]
        suggestions = [
            self._suggestion_projection(item)
            for item in preview_result.get("items", [])
            if isinstance(item, dict)
            and str(item.get("general_situation_id") or "") in situation_ids
        ]
        waiting = self._waiting_projection(
            decision_result.get("items", []),
            situation_ids=situation_ids,
        )
        degraded = any(
            str(result.get("status") or "") not in {"success", "empty"}
            for result in (situation_result, attention_result, preview_result, decision_result)
            if isinstance(result, dict)
        )
        degraded = degraded or malformed_situations
        section_statuses = {
            "situations": section_status(situation_result.get("status"), situations),
            "attention": section_status(attention_result.get("status"), attention),
            "suggestions": section_status(preview_result.get("status"), suggestions),
            "questions": "unsupported",
            "waiting": section_status(decision_result.get("status"), waiting),
        }
        return {
            "schema_version": PRODUCT_TODAY_SCHEMA,
            "status": "degraded" if degraded else "success",
            "scope": scope,
            "goal": self._goal_projection(goal),
            "situations": situations,
            "attention": attention,
            "suggestions": suggestions,
            "questions": {"status": "unsupported", "items": []},
            "waiting": waiting,
            "section_statuses": section_statuses,
            "freshness": self._projection_freshness(),
            "source_summary": {
                "goal": "workspace Goal",
                "situations": "trusted local observations",
                "suggestions": "record-only preview; delivery none",
            },
            "authority": _authority(),
        }


    def matters(
        self,
        *,
        user_id: str,
        session_id: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        today = self.today(user_id=user_id, session_id=session_id, limit=limit)
        commitments, commitment_status = (
            self._commitment_projection_result(user_id, session_id, limit=limit)
            if today.get("status") in {"success", "degraded"}
            else ([], str(today.get("status") or "fail_closed"))
        )
        section_statuses = today.get("section_statuses") if isinstance(today.get("section_statuses"), dict) else {}
        questions = today.get("questions") if isinstance(today.get("questions"), dict) else {}
        question_items = questions.get("items") if isinstance(questions.get("items"), list) else []
        return {
            "schema_version": PRODUCT_MATTERS_SCHEMA,
            "status": matters_status(today.get("status"), {
                **section_statuses,
                "commitments": commitment_status,
            }),
            "scope": today.get("scope"),
            "goal": today.get("goal"),
            "sections": {
                "situations": section_projection(today.get("situations", []), status=section_statuses.get("situations")),
                "attention": section_projection(today.get("attention", []), status=section_statuses.get("attention")),
                "suggestions": section_projection(today.get("suggestions", []), status=section_statuses.get("suggestions")),
                "commitments": section_projection(commitments, status=commitment_status),
                "questions": {
                    "status": _text(questions.get("status"), "unsupported"),
                    "count": len(question_items),
                    "items": question_items,
                },
                "waiting": section_projection(today.get("waiting", []), status=section_statuses.get("waiting")),
            },
            "freshness": today.get("freshness", {}),
            "authority": _authority(),
        }



    def status(self) -> dict[str, Any]:
        sources = {
            "goal": self._source_freshness("user_goals.json"),
            "situations": self._source_freshness("general_situation_state.json"),
            "attention": self._source_freshness("attention_hypothesis_state.json"),
            "suggestions": self._source_freshness("suggestion_outbox.json"),
        }
        agent = self._resolver(self.agent_status_resolver)
        integration = self._resolver(self.integration_status_resolver)
        cognition = self._resolver(self.cognition_status_resolver)
        runtime_build = self._resolver(self.runtime_build_resolver)
        agent_projection = readiness_projection(agent)
        integration_projection = readiness_projection(integration)
        cognition_projection_value = cognition_projection(cognition)
        build_projection_value = build_projection(runtime_build)
        source_degraded = any(value == "unknown" for value in sources.values())
        runtime_degraded = any(
            str(value.get("status") or "") in {"degraded", "unavailable", "error"}
            for value in (agent_projection, integration_projection, cognition_projection_value, build_projection_value)
        )
        return {
            "schema_version": PRODUCT_STATUS_SCHEMA,
            "status": "degraded" if source_degraded or runtime_degraded else "success",
            "product_scope": {
                "mode": "local-first",
                "supported_boundary": "loopback/Tauri",
                "sources": sources,
            },
            "runtime": {
                "build": build_projection_value,
                "agent": agent_projection,
                "integration": integration_projection,
                "cognition": cognition_projection_value,
            },
            "evidence": {
                "implementation": "implemented",
                "configuration": configuration_label(agent_projection, integration_projection),
                "automated": "validation_pending",
                "live": "validation_pending",
                "production": "not_in_scope",
            },
            "authority": _authority(),
            "advanced_available": True,
        }


    # ------------------------------------------------------------------
    # Projection helpers
    # ------------------------------------------------------------------

    def _active_workspace_goals(self) -> list[dict[str, Any]] | None:
        state = self.state_store.read_json("user_goals.json")
        if state.get("_state_corrupt") is True or not isinstance(state.get("goals"), list):
            return None
        result: list[dict[str, Any]] = []
        for item in state["goals"]:
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("schema_version") or "") == WORKSPACE_GOAL_SCHEMA
                and str(item.get("kind") or "") == WORKSPACE_GOAL_KIND
                and str(item.get("source") or "") == WORKSPACE_GOAL_SOURCE
                and str(item.get("status") or "").lower() == "active"
            ):
                if not _text(item.get("user_id")) or not _text(item.get("session_id")):
                    return None
                result.append(item)
        return result

    def _goal_projection(self, goal: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(goal, dict):
            return None
        return {
            "title": _text(goal.get("title"), "Unnamed workspace focus"),
            "description": _text(goal.get("description")),
            "status": _text(goal.get("status"), "active"),
            "priority": goal.get("priority") if isinstance(goal.get("priority"), (int, float)) else None,
            "source": {"kind": "workspace observation", "delivery": "none"},
            "updated_at": _text(goal.get("updated_at")) or None,
        }

    def _situation_projection(self, item: dict[str, Any], *, goal: dict[str, Any]) -> dict[str, Any]:
        status = _text(item.get("status"), "unknown")
        changed = _text(item.get("updated_at")) or _text(item.get("effective_end")) or None
        count = item.get("distinct_event_count")
        known = [
            f"{count} observed change(s)"
            if isinstance(count, int) and not isinstance(count, bool)
            else "Observed changes were recorded, but the count is unknown."
        ]
        return {
            "title": _text(goal.get("title"), "Current situation"),
            "summary": (
                "Veyra is observing recent changes in this focus."
                if status not in {"waiting", "resolved"}
                else f"This focus is currently {status}."
            ),
            "status": status,
            "changed_at": changed,
            "freshness": "fresh" if self._current_item(item) else "stale",
            "known": known,
            "unknown": ["More independent evidence is still needed."],
            "source": {"kind": "trusted local observation", "status": "recorded"},
        }

    @staticmethod
    def _valid_situation(item: dict[str, Any]) -> bool:
        count = item.get("distinct_event_count")
        return not (
            "distinct_event_count" in item
            and (isinstance(count, bool) or not isinstance(count, int))
        )

    def _attention_projection(self, item: dict[str, Any]) -> dict[str, Any]:
        readiness = item.get("attention_readiness")
        value = readiness.get("value") if isinstance(readiness, dict) else None
        unknowns = _list_text(item.get("unknowns"))
        return {
            "title": "Attention hypothesis",
            "status": _text(item.get("status"), "unknown"),
            "why_now": "Recent observed changes make this worth keeping in view.",
            "readiness": value if isinstance(value, (int, float)) else None,
            "unknown": unknowns,
            "freshness": "fresh" if self._current_item(item) else "stale",
            "source": "attention hypothesis (not a fact)",
            "authority": {"external_delivery": False, "execution": False},
        }

    def _suggestion_projection(self, item: dict[str, Any]) -> dict[str, Any]:
        why_now = _list_text(item.get("why_now"), maximum=3)
        evidence = item.get("evidence")
        evidence_count = len(evidence) if isinstance(evidence, list) else 0
        return {
            "kind": "suggestion preview",
            "status": _text(item.get("status"), "recorded"),
            "message": _text(item.get("reason"), "A record-only suggestion was formed."),
            "why_now": why_now or ["A current attention hypothesis supports this suggestion."],
            "known": [f"{evidence_count} supporting observation(s)"],
            "unknown": _list_text(item.get("unknowns")),
            "delivery": "none",
            "preview": True,
            "authority": _authority(),
            "updated_at": _text(item.get("updated_at")) or None,
        }

    @staticmethod
    def _waiting_projection(items: Any, *, situation_ids: set[str]) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        visible: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or str(item.get("general_situation_id") or "") not in situation_ids:
                continue
            if str(item.get("decision_disposition") or "") != "wait":
                continue
            visible.append({
                "status": "waiting",
                "message": "Veyra is waiting for fresher evidence or a clearer next signal.",
                "updated_at": _text(item.get("updated_at")) or None,
            })
            if len(visible) >= 5:
                break
        return visible

    def _commitment_projection_result(self, user_id: str, session_id: str, *, limit: int) -> tuple[list[dict[str, Any]], str]:
        state = self.state_store.read_json("user_commitments.json")
        if state.get("_state_corrupt") is True or not isinstance(state.get("commitments"), list):
            return [], "fail_closed"
        commitments = state["commitments"]
        visible: list[dict[str, Any]] = []
        for item in commitments:
            if not isinstance(item, dict):
                continue
            state_kind, item_user, item_session = owner_scope(item)
            if state_kind != "exact" or item_user != user_id or item_session != session_id:
                continue
            visible.append({
                "title": _text(item.get("title") or item.get("kind"), "Untitled commitment"),
                "status": _text(item.get("status"), "unknown"),
                "next_at": _text(item.get("next_run_at") or item.get("next_refresh_at")) or None,
                "source": "commitment",
            })
        visible = visible[: max(0, min(int(limit), 100))]
        return visible, "success" if visible else "empty"


    def _projection_freshness(self) -> dict[str, str]:
        return {
            "goal": self._source_freshness("user_goals.json"),
            "situations": self._source_freshness("general_situation_state.json"),
            "attention": self._source_freshness("attention_hypothesis_state.json"),
            "suggestions": self._source_freshness("suggestion_outbox.json"),
        }


    def _source_freshness(self, name: str) -> str:
        try:
            return _freshness(self.state_store.read_json(name))
        except Exception:
            return "unknown"

    @staticmethod
    def _current_item(item: dict[str, Any]) -> bool:
        expires = _time(item.get("expires_at"))
        return expires is None or expires >= _now()

    @staticmethod
    def _scope(value: Any, field: str) -> str:
        return normalize_scope_component(value, field)

    @staticmethod
    def _optional_scope(value: Any, field: str) -> str | None:
        if value is None or not str(value).strip():
            return None
        return normalize_scope_component(value, field)

    def _scope_pair(self, user_id: Any, session_id: Any) -> tuple[str, str]:
        return self._scope(user_id, "user_id"), self._scope(session_id, "session_id")

    def _context_error(self, reason: str, *, status: str = "fail_closed") -> dict[str, Any]:
        return {
            "schema_version": PRODUCT_CONTEXT_SCHEMA,
            "status": status,
            "reason": reason,
            "internal_read_scope": None,
            "external_input_scope": None,
            "goal": None,
            "authority": _authority(),
        }

    @staticmethod
    def _resolver(resolver: Callable[[], dict[str, Any]] | None) -> dict[str, Any]:
        if resolver is None:
            return {"status": "not_configured"}
        try:
            value = resolver()
            return deepcopy(value) if isinstance(value, dict) else {"status": "unknown"}
        except Exception as exc:
            return {"status": "unavailable", "reason": type(exc).__name__}

__all__ = [
    "DEFAULT_EXTERNAL_SESSION",
    "LOCAL_EXTERNAL_SESSION",
    "PRODUCT_CONTEXT_SCHEMA",
    "PRODUCT_MATTERS_SCHEMA",
    "PRODUCT_STATUS_SCHEMA",
    "PRODUCT_TODAY_SCHEMA",
    "ProductExperienceService",
]
