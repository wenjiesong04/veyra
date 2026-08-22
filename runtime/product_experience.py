"""User-facing V1 projections over Veyra's semantic Living Context."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from typing import Any, Callable, Mapping

from core.context_scope import owner_scope
from core.goal_store_policy import WORKSPACE_GOAL_KIND, WORKSPACE_GOAL_SCHEMA, WORKSPACE_GOAL_SOURCE
from core.world_state import StateRevisionConflictError, WorldStateStore
from interface.product_living_context import (
    QuestionAnswerRequest,
    QuestionTransitionRequest,
    ReactionFeedbackRequest,
    SourceConsentRequest,
    SourceRevokeRequest,
    SituationCommandRequest,
)
from interface.session_mapper import SessionMapper
from memory_bridge.scope import normalize_scope_component
from runtime.general_situation_runtime import GeneralSituationRuntime
from runtime.living_context_runtime import LivingContextRuntime
from runtime.living_reaction_runtime import LivingReactionRuntime, LivingReactionStorageError
from runtime.product_experience_projection import (
    bounded_strings,
    build_projection,
    cognition_projection,
    configuration_label,
    matters_status,
    question_projection,
    reaction_projection,
    readiness_projection,
    section_projection,
    situation_projection,
)
from runtime.product_living_context import (
    answer_question,
    command_situation,
    defer_question,
    dismiss_question,
)


PRODUCT_CONTEXT_SCHEMA = "veyra.product_context.v1"
PRODUCT_TODAY_SCHEMA = "veyra.product_today.v1"
PRODUCT_MATTERS_SCHEMA = "veyra.product_matters.v1"
PRODUCT_STATUS_SCHEMA = "veyra.product_status.v1"
PRODUCT_SITUATIONS_SCHEMA = "veyra.product_situations.v1"
PRODUCT_SITUATION_SCHEMA = "veyra.product_situation.v1"
PRODUCT_QUESTIONS_SCHEMA = "veyra.product_questions.v1"
PRODUCT_REACTIONS_SCHEMA = "veyra.product_reactions.v1"
PRODUCT_SOURCES_SCHEMA = "veyra.product_sources.v1"
PRODUCT_CHANNEL = "api"
DEFAULT_EXTERNAL_SESSION = "veyra-workspace-primary-v1"
LOCAL_EXTERNAL_SESSION = "veyra-product-local-v1"
TERMINAL_STATUSES = frozenset({"resolved", "expired", "contradicted", "archived", "completed", "closed"})
ACTIVE_STATUSES = frozenset({"emerging", "active", "waiting", "in_progress", "open"})
ACTIVE_NEED_STATUSES = frozenset({"open", "asked", "observing", "waiting"})
DEGRADED_STATUS = "degraded"


def _authority() -> dict[str, bool]:
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
    if isinstance(value, datetime):
        selected = value
    elif isinstance(value, str) and value.strip():
        try:
            selected = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if selected.tzinfo is None or selected.utcoffset() is None:
        return None
    return selected.astimezone(timezone.utc)


def _text(value: Any, fallback: str = "", *, limit: int = 640) -> str:
    selected = value.strip() if isinstance(value, str) else fallback
    return selected[:limit]


def _freshness(value: dict[str, Any], *, now: datetime | None = None) -> str:
    updated = _time(value.get("updated_at"))
    if updated is None:
        return "unknown"
    ttl = value.get("ttl_seconds")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or float(ttl) <= 0:
        return "fresh"
    return "fresh" if ((now or _now()) - updated).total_seconds() <= float(ttl) else "stale"


def _degraded_meta(code: str) -> dict[str, Any]:
    """Return a stable, non-sensitive typed degraded marker.

    Product projections must distinguish unavailable state from an empty
    projection.  Keep the code bounded and server-owned so a malformed state
    cannot leak a filesystem path or exception text through a public GET.
    """

    return {
        "status": DEGRADED_STATUS,
        "code": _text(code, "product_state_unavailable", limit=96),
        "read_only": True,
    }


def _source_status_label(*, enabled: bool, configured: bool, system_permission: str, consented: bool, available: bool) -> str:
    """Project source readiness without collapsing distinct boundaries.

    ``available`` is reserved for a source that can be used by a governed
    read now.  Configuration, OS permission, and user consent remain visible
    as separate fields so a product GET never turns a global runtime ``ok``
    into an item-level ``unknown``/``available`` contradiction.
    """

    if not enabled or not configured:
        return "not_configured"
    permission = str(system_permission or "unknown").strip().lower()
    if permission == "denied":
        return "permission_denied"
    if permission in {"unknown", "not_configured"}:
        return "permission_unknown"
    if not consented:
        return "needs_consent"
    return "available" if available else "unavailable"


class ProductExperienceService:
    """Server-owned V1 product projections and typed command seams."""

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
        living_context_runtime: LivingContextRuntime | None = None,
        reaction_runtime: LivingReactionRuntime | None = None,
        living_context_orchestrator: Any | None = None,
    ) -> None:
        self.state_store = state_store
        self.session_mapper = session_mapper or SessionMapper()
        self.channel = normalize_scope_component(channel, "channel")
        configured_external = external_session_id or os.getenv("VEYRA_PRODUCT_EXTERNAL_SESSION_ID")
        self.external_session_id = _text(configured_external, DEFAULT_EXTERNAL_SESSION, limit=240)
        self.runtime_build_resolver = runtime_build_resolver
        self.agent_status_resolver = agent_status_resolver
        self.integration_status_resolver = integration_status_resolver
        self.cognition_status_resolver = cognition_status_resolver
        self.living_context_orchestrator = living_context_orchestrator
        selected_core = getattr(living_context_orchestrator, "core", None)
        selected_reaction = getattr(living_context_orchestrator, "reaction_runtime", None)
        self.living_context = living_context_runtime or selected_core or LivingContextRuntime(state_store)
        self.reaction_runtime = reaction_runtime or selected_reaction or LivingReactionRuntime(state_store)
        # Legacy/Advanced projections remain available but never substitute for
        # semantic V1 Situations in Today or the new API.
        self.general_situations = GeneralSituationRuntime(state_store)

    def context(self, *, user_id: str | None = None, external_session_id: str | None = None) -> dict[str, Any]:
        requested_user = self._optional_scope(user_id, "user_id")
        goals = self._active_workspace_goals()
        if goals is None:
            return self._context_error("goal_state_unavailable")
        if requested_user is None:
            owners = sorted({str(item.get("user_id") or "") for item in goals if str(item.get("user_id") or "")})
            if len(owners) > 1:
                return self._context_error("ambiguous_active_workspace_goals", status="ambiguous")
            selected_user = owners[0] if owners else "local-user"
        else:
            selected_user = requested_user
        matching = [item for item in goals if str(item.get("user_id") or "") == selected_user]
        if len(matching) > 1:
            return self._context_error("ambiguous_active_workspace_goals", status="ambiguous")
        selected_external = external_session_id
        if selected_external is None:
            selected_external = self.external_session_id if matching else LOCAL_EXTERNAL_SESSION
        external = self._scope(selected_external, "external_session_id")
        mapped = self.session_mapper.map(self.channel, selected_user, external)
        base = {
            "schema_version": PRODUCT_CONTEXT_SCHEMA,
            "channel": self.channel,
            "requested_external_session_id": external,
            "authority": _authority(),
        }
        if matching:
            goal = matching[0]
            goal_session = _text(goal.get("session_id"), limit=240)
            if not goal_session or mapped != goal_session:
                return {
                    **base,
                    "status": "needs_session_link",
                    "reason": "external_session_does_not_match_active_workspace_goal",
                    "internal_read_scope": None,
                    "external_input_scope": {"channel": self.channel, "user_id": selected_user, "session_id": external},
                    "goal": None,
                }
            return {
                **base,
                "status": "ready",
                "reason": None,
                "internal_read_scope": {"user_id": selected_user, "session_id": goal_session},
                "external_input_scope": {"channel": self.channel, "user_id": selected_user, "session_id": external},
                "goal": self._goal_projection(goal),
                "freshness": self._source_freshness("user_goals.json"),
            }
        return {
            **base,
            "status": "empty",
            "reason": "no_active_workspace_goal",
            "internal_read_scope": {"user_id": selected_user, "session_id": mapped},
            "external_input_scope": {"channel": self.channel, "user_id": selected_user, "session_id": external},
            "goal": None,
            "freshness": self._source_freshness("user_goals.json"),
        }

    def list_situations(self, *, user_id: str, session_id: str, limit: int = 20) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        rows, source_status = self._semantic_rows(owner, session, limit=max(1, min(int(limit), 100)))
        active = [row for row in rows if self._status(row) in ACTIVE_STATUSES]
        terminal = [row for row in rows if self._status(row) in TERMINAL_STATUSES]
        result = {
            "schema_version": PRODUCT_SITUATIONS_SCHEMA,
            "status": source_status,
            "scope": {"user_id": owner, "session_id": session},
            "items": [self._situation_projection(row, detail=False) for row in active],
            "recent_terminal": [self._situation_projection(row, detail=False) for row in terminal[:5]],
            "count": len(active),
            "freshness": self._projection_freshness(),
            "authority": _authority(),
        }
        if source_status == DEGRADED_STATUS:
            result["reason"] = "situation_state_integrity_unavailable"
            result["degraded"] = _degraded_meta("situation_state_integrity_unavailable")
        return result

    def situation_detail(self, situation_id: str, *, user_id: str, session_id: str) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        try:
            selected = self.living_context.get_situation(situation_id, owner_id=owner, session_id=session)
        except Exception:
            return {
                "schema_version": PRODUCT_SITUATION_SCHEMA,
                "status": DEGRADED_STATUS,
                "reason": "situation_state_integrity_unavailable",
                "degraded": _degraded_meta("situation_state_integrity_unavailable"),
                "scope": {"user_id": owner, "session_id": session},
                "situation": None,
                "questions": [],
                "reactions": [],
                "freshness": self._projection_freshness(),
                "authority": _authority(),
            }
        if selected is None:
            return {
                "schema_version": PRODUCT_SITUATION_SCHEMA,
                "status": "not_found",
                "scope": {"user_id": owner, "session_id": session},
                "situation": None,
                "freshness": self._projection_freshness(),
                "authority": _authority(),
            }
        if not self._valid_semantic_row(selected):
            return {
                "schema_version": PRODUCT_SITUATION_SCHEMA,
                "status": DEGRADED_STATUS,
                "reason": "situation_state_integrity_unavailable",
                "degraded": _degraded_meta("situation_state_integrity_unavailable"),
                "scope": {"user_id": owner, "session_id": session},
                "situation": None,
                "questions": [],
                "reactions": [],
                "freshness": self._projection_freshness(),
                "authority": _authority(),
            }
        try:
            needs = self.living_context.needs.list(owner_id=owner, session_id=session, situation_id=str(selected["situation_id"]), limit=32)
            reactions = self._reaction_rows(owner, session, situation_id=str(selected["situation_id"]))
        except Exception:
            return {
                "schema_version": PRODUCT_SITUATION_SCHEMA,
                "status": DEGRADED_STATUS,
                "reason": "dependent_product_state_integrity_unavailable",
                "degraded": _degraded_meta("dependent_product_state_integrity_unavailable"),
                "scope": {"user_id": owner, "session_id": session},
                "situation": None,
                "questions": [],
                "reactions": [],
                "freshness": self._projection_freshness(),
                "authority": _authority(),
            }
        current_revision = int(selected["observation_revision"])
        reactions = [row for row in reactions if row.get("situation_revision") == current_revision]
        needs = self._dedupe_active_needs(
            needs,
            owner=owner,
            session=session,
            situation_id=str(selected["situation_id"]),
        )
        return {
            "schema_version": PRODUCT_SITUATION_SCHEMA,
            "status": "success",
            "scope": {"user_id": owner, "session_id": session},
            "situation": self._situation_projection(selected, detail=True),
            "questions": [self._question_projection(item) for item in needs if str(item.get("status") or "") in ACTIVE_NEED_STATUSES],
            "reactions": [self._reaction_projection(item) for item in reactions[:32] if str(item.get("disposition") or "") != "silent"],
            "freshness": self._projection_freshness(),
            "authority": _authority(),
        }

    def command_situation(self, situation_id: str, *, user_id: str, session_id: str, request: SituationCommandRequest) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        result = command_situation(
            self.living_context,
            situation_id,
            owner_id=owner,
            session_id=session,
            request=request,
        )
        result["situation"] = self.situation_detail(situation_id, user_id=owner, session_id=session).get("situation")
        result["authority"] = _authority()
        return result

    def questions(self, *, user_id: str, session_id: str, limit: int = 20) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        try:
            rows = self.living_context.needs.list(owner_id=owner, session_id=session, limit=max(1, min(int(limit), 100)))
            current_ids = {str(item["situation_id"]) for item in self.living_context.list_situations(owner_id=owner, session_id=session, limit=100)}
        except Exception:
            return {
                "schema_version": PRODUCT_QUESTIONS_SCHEMA,
                "status": DEGRADED_STATUS,
                "reason": "dependent_product_state_integrity_unavailable",
                "degraded": _degraded_meta("dependent_product_state_integrity_unavailable"),
                "scope": {"user_id": owner, "session_id": session},
                "items": [],
                "freshness": self._projection_freshness(),
                "authority": _authority(),
            }
        rows = self._dedupe_active_needs(rows, owner=owner, session=session)
        items = [self._question_projection(item) for item in rows if str(item.get("status") or "") in ACTIVE_NEED_STATUSES and str(item.get("situation_id") or "") in current_ids]
        result = {
            "schema_version": PRODUCT_QUESTIONS_SCHEMA,
            "status": "success" if items else "empty",
            "scope": {"user_id": owner, "session_id": session},
            "items": items[: max(0, min(int(limit), 100))],
            "freshness": self._projection_freshness(),
            "authority": _authority(),
        }
        return result

    def answer_question(self, need_id: str, *, user_id: str, session_id: str, request: QuestionAnswerRequest) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        raw = answer_question(self.living_context, need_id, owner_id=owner, session_id=session, request=request)
        return self._question_write_projection(raw, owner=owner, session=session)

    def defer_question(self, need_id: str, *, user_id: str, session_id: str, request: QuestionTransitionRequest) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        raw = defer_question(self.living_context, need_id, owner_id=owner, session_id=session, request=request)
        return self._question_write_projection(raw, owner=owner, session=session)

    def dismiss_question(self, need_id: str, *, user_id: str, session_id: str, request: QuestionTransitionRequest) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        raw = dismiss_question(self.living_context, need_id, owner_id=owner, session_id=session, request=request)
        return self._question_write_projection(raw, owner=owner, session=session)

    def reactions(self, *, user_id: str, session_id: str, situation_id: str | None = None, situation_revision: int | None = None, limit: int = 20, visible_dispositions: set[str] | None = None) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        try:
            # Read the complete bounded ledger first.  Filtering the runtime's
            # newest-N page before current revision filtering can hide a valid
            # current suggestion behind stale rows.
            rows = self._reaction_rows(owner, session, situation_id=situation_id)
        except Exception:
            return {
                "schema_version": PRODUCT_REACTIONS_SCHEMA,
                "status": DEGRADED_STATUS,
                "reason": "reaction_state_integrity_unavailable",
                "degraded": _degraded_meta("reaction_state_integrity_unavailable"),
                "scope": {"user_id": owner, "session_id": session},
                "items": [],
                "silent_items": [],
                "silent_count": 0,
                "freshness": self._projection_freshness(),
                "authority": _authority(),
            }
        try:
            if situation_id is not None:
                current = self.living_context.get_situation(situation_id, owner_id=owner, session_id=session)
                if current is None:
                    raise KeyError(f"unknown Situation: {situation_id}")
                if not self._valid_semantic_row(current):
                    raise ValueError("current Situation is malformed")
                current_revisions = {str(situation_id): int(current["observation_revision"])}
            else:
                repository = getattr(getattr(self.living_context, "situations", None), "repository", None)
                list_rows = getattr(repository, "list", None)
                if callable(list_rows):
                    current_situations = list_rows(
                        user_id=owner,
                        session_id=session,
                        limit=max(1, min(int(getattr(repository, "max_situations", 500)), 500)),
                        semantic_only=True,
                    )
                else:
                    current_situations = self.living_context.list_situations(owner_id=owner, session_id=session, limit=100)
                if not all(isinstance(item, dict) and self._valid_semantic_row(item) for item in current_situations):
                    raise ValueError("current Situation projection is malformed")
                current_revisions = {str(item["situation_id"]): int(item["observation_revision"]) for item in current_situations}
        except KeyError:
            raise
        except Exception:
            return {
                "schema_version": PRODUCT_REACTIONS_SCHEMA,
                "status": DEGRADED_STATUS,
                "reason": "situation_state_integrity_unavailable",
                "degraded": _degraded_meta("situation_state_integrity_unavailable"),
                "scope": {"user_id": owner, "session_id": session},
                "items": [],
                "silent_items": [],
                "silent_count": 0,
                "freshness": self._projection_freshness(),
                "authority": _authority(),
            }
        # This comparison intentionally precedes disposition filtering and
        # every output limit, including the suggestions route.
        rows = [row for row in rows if current_revisions.get(str(row.get("situation_id"))) == int(row.get("situation_revision") or 0)]
        if situation_revision is not None:
            rows = [row for row in rows if row.get("situation_revision") == situation_revision]
        silent_count = sum(str(row.get("disposition") or "") == "silent" for row in rows)
        visible = [self._reaction_projection(row) for row in rows if str(row.get("disposition") or "") != "silent"]
        if visible_dispositions is not None:
            visible = [item for item in visible if item["disposition"] in visible_dispositions]
        silent = [self._reaction_projection(row) for row in rows if str(row.get("disposition") or "") == "silent"]
        if visible_dispositions is not None:
            silent, silent_count = [], 0
        result = {
            "schema_version": PRODUCT_REACTIONS_SCHEMA,
            "status": "success" if visible else "empty",
            "scope": {"user_id": owner, "session_id": session},
            "items": visible[: max(0, min(int(limit), 200))],
            "silent_items": silent[: max(0, min(int(limit), 200))],
            "silent_count": silent_count,
            "freshness": self._projection_freshness(),
            "authority": _authority(),
        }
        return result
    def feedback_reaction(self, reaction_id: str, *, user_id: str, session_id: str, request: ReactionFeedbackRequest) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        exact_getter = getattr(self.reaction_runtime, "get_reaction", None)
        if callable(exact_getter):
            reaction = exact_getter(reaction_id, owner_id=owner, session_id=session)
        else:
            rows = self.reaction_runtime.list_reactions(owner_id=owner, session_id=session, limit=200)
            reaction = next((row for row in rows if str(row.get("reaction_id") or "") == str(reaction_id)), None)
        if reaction is None:
            raise KeyError(f"unknown reaction: {reaction_id}")
        situation = self.living_context.get_situation(str(reaction.get("situation_id") or ""), owner_id=owner, session_id=session)
        if situation is None:
            raise KeyError("reaction Situation is unavailable")
        current_revision = int(situation["observation_revision"])
        if int(reaction.get("situation_revision") or 0) != current_revision:
            raise StateRevisionConflictError("reaction is stale for the current Situation revision")
        if request.situation_revision != current_revision:
            raise StateRevisionConflictError("reaction Situation revision does not match")
        payload = {
            "feedback_id": f"product_feedback_{reaction_id}_{request.label}_{request.situation_revision}",
            "owner_id": owner,
            "session_id": session,
            "situation_id": reaction["situation_id"],
            "reaction_id": reaction_id,
            "label": request.label,
            "category": request.category or reaction.get("category") or "general",
            "remind_before_seconds": request.remind_before_seconds,
            "evidence_refs": list(request.evidence_refs),
            "now": _now().isoformat(),
        }
        result = self.reaction_runtime.record_feedback(payload)
        result["authority"] = _authority()
        return result
    def sources(self, *, user_id: str, session_id: str) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        default = {
            name: {
                "status": "not_configured",
                "enabled": False,
                "configured": False,
                "system_permission": "not_configured",
                "consented": False,
                "available": False,
                "attemptable": False,
                "can_request": False,
                "evidence_level": "pending",
            }
            for name in ("calendar", "weather", "public_web", "user")
        }
        if self.living_context_orchestrator is None:
            # Source status has one owner: the Living Context orchestrator.  A
            # service assembled without that facade must remain fail-closed;
            # it must not infer source readiness from a second callback path.
            return {
                "schema_version": PRODUCT_SOURCES_SCHEMA,
                "status": "fail_closed",
                "scope": {"user_id": owner, "session_id": session},
                "items": default,
                "authority": _authority(),
            }
        try:
            raw_status = self.living_context_orchestrator.source_status(owner_id=owner, session_id=session)
            if not isinstance(raw_status, dict):
                raise TypeError("Living Context source status must be an object")
            source_items = raw_status.get("capabilities")
            consent_items = raw_status.get("consent")
            if not isinstance(source_items, Mapping) or not isinstance(consent_items, Mapping):
                raise TypeError("Living Context source status is incomplete")
            source_keys = {"calendar": "calendar", "weather": "weather", "public_web": "public_web", "user": "user_answer"}
            items: dict[str, dict[str, Any]] = {}
            for name, source_key in source_keys.items():
                capability = source_items.get(source_key)
                consent = consent_items.get(source_key)
                if not isinstance(capability, Mapping):
                    capability = {}
                if not isinstance(consent, Mapping):
                    consent = {}
                enabled = bool(capability.get("enabled", False))
                configured = bool(capability.get("configured", False))
                permission = _text(capability.get("system_permission"), "unknown", limit=32).lower()
                consented = bool(consent.get("granted", capability.get("consented", not bool(consent.get("required", False)))))
                available = bool(capability.get("available", False) and consented)
                can_request = bool(capability.get("can_request", capability.get("attemptable", False)) and consented)
                item_status = _source_status_label(
                    enabled=enabled,
                    configured=configured,
                    system_permission=permission,
                    consented=consented,
                    available=available,
                )
                items[name] = {
                    "status": item_status,
                    "enabled": enabled,
                    "configured": configured,
                    "system_permission": permission,
                    "consent_required": bool(consent.get("required", capability.get("consent_required", False))),
                    "available": available,
                    "attemptable": can_request,
                    "can_request": can_request,
                    "consented": consented,
                    "consent_id": str(consent.get("consent_id") or "") or None,
                    "consent_generation": int(consent.get("generation") or 0),
                    "evidence_level": "available" if available else "configured" if configured else "pending",
                }
            return {
                "schema_version": PRODUCT_SOURCES_SCHEMA,
                "status": "success" if raw_status.get("status") in {"ok", "success"} else "degraded",
                "scope": {"user_id": owner, "session_id": session},
                "items": items,
                "authority": _authority(),
            }
        except Exception:
            return {
                "schema_version": PRODUCT_SOURCES_SCHEMA,
                "status": DEGRADED_STATUS,
                "reason": "source_projection_integrity_unavailable",
                "degraded": _degraded_meta("source_projection_integrity_unavailable"),
                "scope": {"user_id": owner, "session_id": session},
                "items": {},
                "authority": _authority(),
            }

    def consent_source(self, source: str, *, user_id: str, session_id: str, request: SourceConsentRequest) -> dict[str, Any]:
        if self.living_context_orchestrator is None:
            raise RuntimeError("Living Context orchestrator is not configured")
        owner, session = self._scope_pair(user_id, session_id)
        return self.living_context_orchestrator.grant_source_consent(
            source,
            owner_id=owner,
            session_id=session,
            purpose=request.purpose,
            expected_generation=request.expected_generation,
            consent_id=request.consent_id,
            expires_at=request.expires_at,
        )

    def revoke_source(self, source: str, *, user_id: str, session_id: str, request: SourceRevokeRequest) -> dict[str, Any]:
        if self.living_context_orchestrator is None:
            raise RuntimeError("Living Context orchestrator is not configured")
        owner, session = self._scope_pair(user_id, session_id)
        return self.living_context_orchestrator.revoke_source_consent(
            source,
            owner_id=owner,
            session_id=session,
            expected_generation=request.expected_generation,
            consent_id=request.consent_id,
        )

    def today(self, *, user_id: str, session_id: str, limit: int = 20, first_meeting: bool = False) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        bound = max(1, min(int(limit), 100))
        situation_rows, situation_status = self._semantic_rows(owner, session, limit=bound)
        projected = [self._situation_projection(row, detail=False) for row in situation_rows]
        active = [item for item in projected if item["status"] in ACTIVE_STATUSES]
        recent_terminal = [item for item in projected if item["status"] in TERMINAL_STATUSES][:5]
        question_result = self.questions(user_id=owner, session_id=session, limit=bound)
        reaction_result = self.reactions(user_id=owner, session_id=session, limit=bound, visible_dispositions={"suggest", "wait"})
        current_reactions = self.reactions(user_id=owner, session_id=session, limit=max(bound, 50))
        attention = self._attention_rows(active, current_reactions.get("items", []), limit=bound)
        deadlines = [item for item in active if item.get("deadline_at")]
        deadlines.sort(key=lambda item: str(item.get("deadline_at") or ""))
        # ``changed_at`` is a bookkeeping timestamp, not evidence of a
        # material change.  Only a non-empty semantic material_change is
        # allowed into the user-facing Recent changes section.
        recent_changes = [
            item
            for item in active + recent_terminal
            if isinstance(item.get("material_change"), str) and item["material_change"].strip()
        ]
        recent_changes.sort(key=lambda item: str(item.get("changed_at") or ""), reverse=True)
        recent_changes = recent_changes[:bound]
        unknowns: list[dict[str, Any]] = []
        for item in active:
            for unknown in item.get("unknown", [])[:4]:
                unknowns.append({"situation_id": item["situation_id"], "statement": unknown})
        goal = self._goal_for_scope(owner, session)
        legacy = self._legacy_sections(owner, session, bound) if not active and situation_status in {"success", "empty"} else {"status": "not_primary", "items": []}
        degraded = situation_status in {"fail_closed", DEGRADED_STATUS} or question_result["status"] in {"fail_closed", DEGRADED_STATUS} or reaction_result["status"] in {"fail_closed", DEGRADED_STATUS} or legacy.get("status") in {"fail_closed", DEGRADED_STATUS}
        status = "degraded" if degraded else "success" if active or question_result["items"] or reaction_result["items"] or attention else "empty"
        result = {
            "schema_version": PRODUCT_TODAY_SCHEMA,
            "status": status,
            "scope": {"user_id": owner, "session_id": session},
            "first_meeting": bool(
                first_meeting
                and situation_status == "empty"
                and question_result["status"] not in {"fail_closed", "degraded"}
                and reaction_result["status"] not in {"fail_closed", "degraded"}
                and not question_result["items"]
            ),
            "focus": goal,
            "goal": goal,
            "situations": active,
            "recent_terminal": recent_terminal,
            "recent_changes": recent_changes,
            "deadlines": deadlines[:bound],
            "unknowns": unknowns[:bound],
            "questions": question_result,
            "suggestions": [item for item in reaction_result["items"] if item["disposition"] == "suggest"],
            "reactions": reaction_result,
            "attention": attention,
            "waiting": [item for item in reaction_result["items"] if item.get("disposition") == "wait"],
            "legacy": legacy,
            "section_statuses": {
                "situations": situation_status if situation_status != "success" or active else "empty",
                "recent_changes": "success" if recent_changes else "empty",
                "deadlines": "success" if deadlines else "empty",
                "unknowns": "success" if unknowns else "empty",
                "questions": question_result["status"],
                "suggestions": "success" if any(item["disposition"] == "suggest" for item in reaction_result["items"]) else "empty",
                "attention": "success" if attention else "empty",
            },
            "freshness": self._projection_freshness(),
            "source_summary": {
                "semantic": "situation_state.json via SituationStateRepository (SemanticSituationRuntime; legacy SituationEvaluator)",
                "information_needs": "InformationNeedRuntime",
                "reactions": "LivingReactionRuntime; silent rows are not notifications",
                "focus": "optional Workspace Goal",
            },
            "authority": _authority(),
        }
        if status == DEGRADED_STATUS:
            result["reason"] = "product_projection_integrity_unavailable"
            result["degraded"] = _degraded_meta("product_projection_integrity_unavailable")
        return result

    def matters(self, *, user_id: str, session_id: str, limit: int = 20) -> dict[str, Any]:
        owner, session = self._scope_pair(user_id, session_id)
        today = self.today(user_id=owner, session_id=session, limit=limit)
        commitments, commitment_status = self._commitment_projection_result(owner, session, limit=limit)
        result = {
            "schema_version": PRODUCT_MATTERS_SCHEMA,
            "status": matters_status(today.get("status"), {**(today.get("section_statuses") or {}), "commitments": commitment_status}),
            "scope": today.get("scope"),
            "focus": today.get("focus"),
            "goal": today.get("goal"),
            "sections": {
                "situations": section_projection(today.get("situations", []), status=(today.get("section_statuses") or {}).get("situations")),
                "recent_changes": section_projection(today.get("recent_changes", []), status=(today.get("section_statuses") or {}).get("recent_changes")),
                "deadlines": section_projection(today.get("deadlines", []), status=(today.get("section_statuses") or {}).get("deadlines")),
                "unknowns": section_projection(today.get("unknowns", []), status=(today.get("section_statuses") or {}).get("unknowns")),
                "questions": section_projection((today.get("questions") or {}).get("items", []), status=(today.get("questions") or {}).get("status")),
                "suggestions": section_projection(today.get("suggestions", []), status=(today.get("section_statuses") or {}).get("suggestions")),
                "commitments": section_projection(commitments, status=commitment_status),
                "waiting": section_projection(today.get("waiting", []), status="success" if today.get("waiting") else "empty"),
            },
            "freshness": today.get("freshness", {}),
            "authority": _authority(),
        }
        if result["status"] == DEGRADED_STATUS:
            result["reason"] = "product_projection_integrity_unavailable"
            result["degraded"] = _degraded_meta("product_projection_integrity_unavailable")
        return result

    def status(self) -> dict[str, Any]:
        sources = {
            "goal": self._source_freshness("user_goals.json"),
            "situations": self._source_freshness("situation_state.json"),
            "information_needs": self._source_freshness("information_need_state.json"),
            "reactions": self._source_freshness("living_reaction_state.json"),
            "legacy_situations": self._source_freshness("general_situation_state.json"),
        }
        agent = self._resolver(self.agent_status_resolver)
        integration = self._resolver(self.integration_status_resolver)
        cognition = self._resolver(self.cognition_status_resolver)
        runtime_build = self._resolver(self.runtime_build_resolver)
        agent_projection = readiness_projection(agent)
        integration_projection = readiness_projection(integration)
        cognition_projection_value = cognition_projection(cognition)
        build_projection_value = build_projection(runtime_build)
        integrity = self._integrity_projection()
        runtime_degraded = any(str(value.get("status") or "") in {"degraded", "unavailable", "error"} for value in (agent_projection, integration_projection, cognition_projection_value, build_projection_value))
        status = DEGRADED_STATUS if runtime_degraded or integrity["status"] == DEGRADED_STATUS else "success"
        return {
            "schema_version": PRODUCT_STATUS_SCHEMA,
            "status": status,
            "product_scope": {"mode": "local-first", "supported_boundary": "loopback/Tauri", "sources": sources},
            "runtime": {"build": build_projection_value, "agent": agent_projection, "integration": integration_projection, "cognition": cognition_projection_value},
            "evidence": {"implementation": "implemented", "configuration": configuration_label(agent_projection, integration_projection), "automated": "validation_pending", "live": "validation_pending", "production": "not_in_scope"},
            "integrity": integrity,
            "authority": _authority(),
            "advanced_available": True,
        }

    def _integrity_projection(self) -> dict[str, Any]:
        """Validate product state in memory only; never repair or migrate on GET."""

        checks = {
            "goals": self._integrity_check("goals"),
            "situations": self._integrity_check("situations"),
            "information_needs": self._integrity_check("information_needs"),
            "reactions": self._integrity_check("reactions"),
            "legacy_situations": self._integrity_check("legacy_situations"),
        }
        degraded = any(item.get("status") == DEGRADED_STATUS for item in checks.values())
        return {"status": DEGRADED_STATUS if degraded else "success", "read_only": True, "checks": checks}

    def _integrity_check(self, name: str) -> dict[str, Any]:
        files = {
            "goals": "user_goals.json",
            "situations": "situation_state.json",
            "information_needs": "information_need_state.json",
            "reactions": "living_reaction_state.json",
            "legacy_situations": "general_situation_state.json",
        }
        state_file = files[name]
        try:
            raw = self.state_store.read_json(state_file)
            path_for = getattr(self.state_store, "path_for", None)
            path = path_for(state_file) if callable(path_for) else None
            present = bool(path is not None and path.exists())
            if not raw and not present:
                return {"status": "empty", "read_only": True}
            if not isinstance(raw, dict) or raw.get("_state_corrupt") is True:
                return {"status": DEGRADED_STATUS, "code": f"{name}_state_corrupt", "read_only": True}
            if name == "situations":
                repository = getattr(getattr(self.living_context, "situations", None), "repository", None)
                validator = getattr(repository, "validate", None)
                if not callable(validator):
                    raise ValueError("Situation validator unavailable")
                validator(deepcopy(raw))
            elif name == "information_needs":
                validator = getattr(getattr(self.living_context, "needs", None), "_assert_state", None)
                if not callable(validator):
                    raise ValueError("InformationNeed validator unavailable")
                validator(deepcopy(raw))
            elif name == "reactions":
                validator = getattr(self.reaction_runtime, "_ensure_state", None)
                if not callable(validator):
                    raise ValueError("reaction validator unavailable")
                validator(deepcopy(raw))
            elif name == "goals":
                goals = raw.get("goals")
                if not isinstance(goals, list):
                    raise ValueError("workspace goal index is invalid")
                for goal in goals:
                    if not isinstance(goal, dict):
                        raise ValueError("workspace goal row is invalid")
                    if str(goal.get("status") or "").lower() == "active":
                        if not _text(goal.get("user_id")) or not _text(goal.get("session_id")):
                            raise ValueError("workspace goal scope is incomplete")
            else:
                rows = raw.get("situations", raw.get("records", []))
                if not isinstance(rows, list):
                    raise ValueError("legacy Situation index is invalid")
            return {"status": "valid", "read_only": True}
        except Exception:
            return {"status": DEGRADED_STATUS, "code": f"{name}_state_integrity_unavailable", "read_only": True}

    def _semantic_rows(self, owner: str, session: str, *, limit: int) -> tuple[list[dict[str, Any]], str]:
        try:
            rows = self.living_context.list_situations(
                owner_id=owner,
                session_id=session,
                limit=max(1, min(int(limit), 100)),
            )
        except Exception:
            return [], DEGRADED_STATUS
        if not all(isinstance(item, dict) and self._valid_semantic_row(item) for item in rows):
            return [], DEGRADED_STATUS
        rows = [deepcopy(item) for item in rows]
        return rows[:limit], "success" if rows else "empty"

    def _reaction_rows(self, owner: str, session: str, *, situation_id: str | None = None) -> list[dict[str, Any]]:
        """Read the validated hot+archive projection without mutating state.

        The runtime owns exact archive replay and returns a bounded product
        projection only after scope filtering; the current Situation revision
        fence is applied by the caller before any UI limit.
        """

        list_reactions = getattr(self.reaction_runtime, "list_reactions", None)
        if callable(list_reactions):
            rows = list_reactions(
                owner_id=owner,
                session_id=session,
                situation_id=situation_id,
                limit=10000,
            )
        else:
            read_state = getattr(self.reaction_runtime, "read_state", None)
            state = read_state() if callable(read_state) else {}
            reactions = state.get("reactions") if isinstance(state, dict) else None
            if not isinstance(reactions, dict):
                raise LivingReactionStorageError("reaction state reactions index is invalid")
            rows = list(reactions.values())
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise LivingReactionStorageError("reaction projection is invalid")
        rows = [
            deepcopy(row)
            for row in rows
            if str(row.get("owner_id") or "") == owner
            and str(row.get("session_id") or "") == session
            and (situation_id is None or str(row.get("situation_id") or "") == situation_id)
        ]
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows

    @staticmethod
    def _attention_rows(
        situations: list[dict[str, Any]],
        reactions: Any,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Build the server-owned Today attention ranking.

        Attention is a compact projection of independent signals rather than
        a second notification ledger: deadline proximity, material change,
        unresolved unknowns, and the current reaction for the exact Situation
        revision. Suggestions remain separately filtered to ``suggest``.
        """

        rows = reactions if isinstance(reactions, list) else []
        by_situation: dict[str, dict[str, Any]] = {}
        for reaction in rows:
            if not isinstance(reaction, dict):
                continue
            situation_id = str(reaction.get("situation_id") or "")
            if not situation_id or str(reaction.get("disposition") or "") == "silent":
                continue
            revision = int(reaction.get("situation_revision") or 0)
            previous = by_situation.get(situation_id)
            if previous is None or revision > int(previous.get("situation_revision") or 0):
                by_situation[situation_id] = reaction

        attention: list[dict[str, Any]] = []
        now = _now()
        for situation in situations:
            if not isinstance(situation, dict):
                continue
            situation_id = str(situation.get("situation_id") or "")
            if not situation_id:
                continue
            deadline = _time(situation.get("deadline_at"))
            deadline_score = 0.0
            if deadline is not None:
                seconds = (deadline - now).total_seconds()
                if seconds <= 0:
                    deadline_score = 1.0
                elif seconds <= 6 * 3600:
                    deadline_score = 0.94
                elif seconds <= 24 * 3600:
                    deadline_score = 0.84
                elif seconds <= 3 * 86400:
                    deadline_score = 0.70
                elif seconds <= 7 * 86400:
                    deadline_score = 0.52
            material = bool(str(situation.get("material_change") or "").strip())
            unknown = [str(item) for item in (situation.get("unknown") or []) if str(item).strip()]
            reaction = by_situation.get(situation_id)
            reaction_rank = float(reaction.get("rank") or 0.0) if reaction else 0.0
            score = min(1.0, round(
                0.42 * deadline_score
                + 0.24 * (1.0 if material else 0.0)
                + 0.16 * min(1.0, len(unknown) / 3.0)
                + 0.18 * max(0.0, min(1.0, reaction_rank)),
                4,
            ))
            if score <= 0.0:
                continue
            signals: list[str] = []
            if deadline_score:
                signals.append("deadline")
            if material:
                signals.append("material_change")
            if unknown:
                signals.append("unknown")
            if reaction is not None:
                signals.append("current_reaction")
            why_now = str((reaction or {}).get("why_now") or "").strip()
            if not why_now:
                if deadline_score:
                    why_now = "A relevant deadline is close enough to affect the next step."
                elif material:
                    why_now = "A material change was recorded for this Situation."
                else:
                    why_now = "An unresolved unknown may affect the current understanding."
            attention.append({
                "situation_id": situation_id,
                "revision": int(situation.get("revision") or 1),
                "title": str(situation.get("title") or situation.get("label") or "Situation")[:240],
                "summary": str(situation.get("summary") or "")[:640],
                "status": str(situation.get("status") or "active")[:48],
                "rank": score,
                "signals": signals,
                "why_now": why_now[:480],
                "deadline_at": situation.get("deadline_at"),
                "material_change": str(situation.get("material_change") or "")[:480],
                "unknown": unknown[:4],
                "reaction": reaction,
            })
        attention.sort(key=lambda item: (-float(item.get("rank") or 0.0), str(item.get("deadline_at") or "9999"), str(item.get("situation_id") or "")))
        return attention[: max(0, min(int(limit), 100))]

    @staticmethod
    def _valid_semantic_row(item: dict[str, Any]) -> bool:
        semantic = item.get("semantic")
        revision = item.get("observation_revision")
        return bool(
            str(item.get("situation_id") or "")
            and str(item.get("user_id") or "")
            and str(item.get("session_id") or "")
            and isinstance(semantic, dict)
            and isinstance(revision, int)
            and not isinstance(revision, bool)
            and revision >= 1
        )

    @staticmethod
    def _status(item: Mapping[str, Any]) -> str:
        return _text(item.get("status") or (item.get("semantic") or {}).get("lifecycle"), "unknown", limit=48).lower()

    @staticmethod
    def _situation_projection(item: dict[str, Any], *, detail: bool) -> dict[str, Any]:
        return situation_projection(item, detail=detail)

    @staticmethod
    def _question_projection(item: dict[str, Any]) -> dict[str, Any]:
        return question_projection(item)

    @staticmethod
    def _valid_need_binding_digest(item: Mapping[str, Any]) -> str | None:
        """Return a server-owned Need binding digest, if the row proves it."""

        binding = item.get("unknown_binding")
        digest = item.get("unknown_binding_digest")
        if not isinstance(binding, str) or not binding:
            return None
        if not isinstance(digest, str) or len(digest) != 64 or digest.lower() != digest:
            return None
        if any(character not in "0123456789abcdef" for character in digest):
            return None
        expected = hashlib.sha256(binding.encode("utf-8")).hexdigest()
        return digest if digest == expected else None

    @classmethod
    def _dedupe_active_needs(
        cls,
        rows: Any,
        *,
        owner: str,
        session: str,
        situation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Deduplicate bound active Needs for the Product read model only.

        Durable InformationNeed state is intentionally untouched.  Only rows
        with a server-validated binding digest participate, and the scope key
        includes the exact owner, session, and Situation identity.
        """

        if not isinstance(rows, list):
            return []

        status_priority = {
            "asked": 0,
            "observing": 0,
            "waiting": 1,
            "open": 1,
        }
        selected: dict[tuple[str, str, str, str], tuple[int, dict[str, Any]]] = {}
        duplicate_indexes: set[int] = set()

        def canonical_key(row: Mapping[str, Any]) -> tuple[int, datetime, str]:
            created_at = _time(row.get("created_at"))
            created_key = created_at or datetime.max.replace(tzinfo=timezone.utc)
            return (
                status_priority.get(str(row.get("status") or "").lower(), 2),
                created_key,
                str(row.get("need_id") or ""),
            )

        for index, raw in enumerate(rows):
            if not isinstance(raw, dict):
                continue
            if str(raw.get("status") or "").lower() not in ACTIVE_NEED_STATUSES:
                continue
            if (
                not isinstance(raw.get("owner_id"), str)
                or raw.get("owner_id") != owner
                or not isinstance(raw.get("session_id"), str)
                or raw.get("session_id") != session
                or not isinstance(raw.get("situation_id"), str)
                or (situation_id is not None and raw.get("situation_id") != situation_id)
            ):
                continue
            digest = cls._valid_need_binding_digest(raw)
            if digest is None:
                continue
            key = (owner, session, str(raw["situation_id"]), digest)
            previous = selected.get(key)
            if previous is None:
                selected[key] = (index, raw)
                continue
            if canonical_key(raw) < canonical_key(previous[1]):
                duplicate_indexes.add(previous[0])
                selected[key] = (index, raw)
            else:
                duplicate_indexes.add(index)

        return [deepcopy(raw) for index, raw in enumerate(rows) if index not in duplicate_indexes and isinstance(raw, dict)]

    @staticmethod
    def _reaction_projection(item: dict[str, Any]) -> dict[str, Any]:
        return reaction_projection(item)

    def _question_write_projection(self, raw: dict[str, Any], *, owner: str, session: str) -> dict[str, Any]:
        result = {"status": raw.get("status", "recorded"), "authority": _authority()}
        if isinstance(raw.get("need"), dict):
            result["need"] = self._question_projection(raw["need"])
        if isinstance(raw.get("situation"), dict):
            result["situation"] = self._situation_projection(raw["situation"], detail=True)
        return result

    def _goal_for_scope(self, owner: str, session: str) -> dict[str, Any] | None:
        goals = self._active_workspace_goals()
        if not goals:
            return None
        matching = [item for item in goals if str(item.get("user_id") or "") == owner and str(item.get("session_id") or "") == session]
        return self._goal_projection(matching[0]) if len(matching) == 1 else None

    def _legacy_sections(self, owner: str, session: str, limit: int) -> dict[str, Any]:
        try:
            result = self.general_situations.list_for_owner(user_id=owner, session_id=session, limit=limit)
            items = result.get("items", []) if isinstance(result, dict) and isinstance(result.get("items"), list) else []
            return {"status": str(result.get("status") or "empty"), "items": items[:limit]}
        except Exception:
            return {"status": DEGRADED_STATUS, "items": [], "degraded": _degraded_meta("legacy_situation_state_unavailable")}

    def _active_workspace_goals(self) -> list[dict[str, Any]] | None:
        state = self.state_store.read_json("user_goals.json")
        if state.get("_state_corrupt") is True or not isinstance(state.get("goals"), list):
            return None
        result: list[dict[str, Any]] = []
        for item in state["goals"]:
            if not isinstance(item, dict):
                continue
            if str(item.get("schema_version") or "") == WORKSPACE_GOAL_SCHEMA and str(item.get("kind") or "") == WORKSPACE_GOAL_KIND and str(item.get("source") or "") == WORKSPACE_GOAL_SOURCE and str(item.get("status") or "").lower() == "active":
                if not _text(item.get("user_id")) or not _text(item.get("session_id")):
                    return None
                result.append(item)
        return result

    @staticmethod
    def _goal_projection(goal: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(goal, dict):
            return None
        return {
            "title": _text(goal.get("title"), "Unnamed workspace focus", limit=240),
            "description": _text(goal.get("description"), limit=640),
            "status": _text(goal.get("status"), "active", limit=32),
            "priority": goal.get("priority") if isinstance(goal.get("priority"), (int, float)) and not isinstance(goal.get("priority"), bool) else None,
            "source": {"kind": "workspace observation", "delivery": "none"},
            "updated_at": _text(goal.get("updated_at")) or None,
        }

    def _commitment_projection_result(self, user_id: str, session_id: str, *, limit: int) -> tuple[list[dict[str, Any]], str]:
        state = self.state_store.read_json("user_commitments.json")
        if state.get("_state_corrupt") is True or not isinstance(state.get("commitments"), list):
            return [], DEGRADED_STATUS
        visible: list[dict[str, Any]] = []
        for item in state["commitments"]:
            if not isinstance(item, dict):
                continue
            state_kind, item_user, item_session = owner_scope(item)
            if state_kind != "exact" or item_user != user_id or item_session != session_id:
                continue
            visible.append({"title": _text(item.get("title") or item.get("kind"), "Untitled commitment", limit=240), "status": _text(item.get("status"), "unknown", limit=32), "next_at": _text(item.get("next_run_at") or item.get("next_refresh_at")) or None, "source": "commitment"})
        selected = visible[: max(0, min(int(limit), 100))]
        return selected, "success" if selected else "empty"

    def _projection_freshness(self) -> dict[str, str]:
        return {
            "situations": self._source_freshness("situation_state.json"),
            "information_needs": self._source_freshness("information_need_state.json"),
            "reactions": self._source_freshness("living_reaction_state.json"),
            "focus": self._source_freshness("user_goals.json"),
        }

    def _source_freshness(self, name: str) -> str:
        try:
            return _freshness(self.state_store.read_json(name))
        except Exception:
            return "unknown"

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
        return {"schema_version": PRODUCT_CONTEXT_SCHEMA, "status": status, "reason": reason, "internal_read_scope": None, "external_input_scope": None, "goal": None, "authority": _authority()}

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
    "PRODUCT_QUESTIONS_SCHEMA",
    "PRODUCT_REACTIONS_SCHEMA",
    "PRODUCT_SITUATIONS_SCHEMA",
    "PRODUCT_SITUATION_SCHEMA",
    "PRODUCT_SOURCES_SCHEMA",
    "PRODUCT_STATUS_SCHEMA",
    "PRODUCT_TODAY_SCHEMA",
    "ProductExperienceService",
]
