"""Private, exact-scope creation of one long-running workspace Goal.

This is deliberately narrower than a general Goal service.  It exists so a
real user session can opt a current local workspace into the record-only
TrustedWorkspaceObserver without editing durable JSON by hand.  The control
never grants delivery, execution, Agent, Tool, Route, or Risk authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
from typing import Any

from core.world_state import WorldStateStore
from core.goal_store_policy import (
    WORKSPACE_GOAL_KIND,
    WORKSPACE_GOAL_SCHEMA,
    WORKSPACE_GOAL_SOURCE,
    retain_shared_goals,
)
from interface.event_schema import utc_now_iso
from memory_bridge.scope import normalize_scope_component


CONTROL_TOKEN_ENV = "VEYRA_LOCAL_API_TOKEN"
GOAL_SCHEMA_VERSION = WORKSPACE_GOAL_SCHEMA
CREATE_SCHEMA_VERSION = "veyra.workspace_goal.create.v1"
SOURCE = WORKSPACE_GOAL_SOURCE
KIND = WORKSPACE_GOAL_KIND
MAX_OPERATIONS = 256
MAX_WORKSPACE_GOALS = 16

_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")


class WorkspaceGoalError(ValueError):
    """Base rejection for the private workspace Goal control."""


class WorkspaceGoalUnauthorized(WorkspaceGoalError):
    """Raised when the local control token is absent or wrong."""


class WorkspaceGoalConflict(WorkspaceGoalError):
    """Raised for CAS, scope, replay, or durable-state conflicts."""


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class WorkspaceGoalControl:
    """Create and read one active observer Goal per exact workspace scope."""

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        control_token: str | None = None,
    ) -> None:
        self.state_store = state_store
        self.control_token = str(
            os.getenv(CONTROL_TOKEN_ENV, "")
            if control_token is None
            else control_token
        ).strip()

    @staticmethod
    def authority() -> dict[str, bool]:
        return {
            "route_change": False,
            "agent_dispatch": False,
            "tool_call": False,
            "capability_grant": False,
            "external_delivery": False,
            "execution": False,
            "fact_certification": False,
            "risk_change": False,
        }

    def create(
        self,
        *,
        control_token: str,
        operation_id: str,
        expected_state_revision: int,
        user_id: str,
        session_id: str,
        workspace_id: str,
        title: str,
        description: str | None,
        priority: float,
    ) -> dict[str, Any]:
        self._authorize(control_token)
        operation = self._operation(operation_id)
        if (
            isinstance(expected_state_revision, bool)
            or not isinstance(expected_state_revision, int)
            or expected_state_revision < 0
        ):
            raise WorkspaceGoalConflict("invalid Goal CAS revision")
        selected_user = normalize_scope_component(user_id, "user_id")
        selected_session = normalize_scope_component(session_id, "session_id")
        workspace = self._workspace(workspace_id)
        workspace_ref = self._workspace_ref(workspace)
        selected_title = self._text(title, "title", maximum=160)
        selected_description = self._optional_text(
            description,
            "description",
            maximum=1000,
        )
        selected_priority = self._priority(priority)
        semantic = {
            "schema_version": CREATE_SCHEMA_VERSION,
            "operation_id": operation,
            "user_id": selected_user,
            "session_id": selected_session,
            "workspace_ref": workspace_ref,
            "title": selected_title,
            "description": selected_description,
            "priority": selected_priority,
        }
        semantic_digest = _digest(semantic)
        goal_id = "wgoal_" + _digest(
            {
                "namespace": GOAL_SCHEMA_VERSION,
                "operation_id": operation,
                "user_id": selected_user,
                "session_id": selected_session,
                "workspace_ref": workspace_ref,
            }
        )[:24]
        result: dict[str, Any] = {}

        operation_key = self._operation_key(
            operation_id=operation,
            user_id=selected_user,
            session_id=selected_session,
        )

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal result
            if state.get("_state_corrupt") is True:
                raise WorkspaceGoalConflict("Goal state is corrupt")
            goals = state.get("goals")
            if not isinstance(goals, list) or any(
                not isinstance(item, dict) for item in goals
            ):
                raise WorkspaceGoalConflict("Goal state is invalid")
            operations = state.get("workspace_goal_operations")
            if operations is None:
                operations = {}
            if not isinstance(operations, dict) or any(
                not isinstance(key, str) or not isinstance(value, dict)
                for key, value in operations.items()
            ):
                raise WorkspaceGoalConflict("workspace Goal operation state is invalid")

            previous = operations.get(operation_key)
            if isinstance(previous, dict):
                if str(previous.get("semantic_digest") or "") != semantic_digest:
                    raise WorkspaceGoalConflict("workspace Goal operation was rebound")
                existing = self._goal_by_id(goals, str(previous.get("goal_id") or ""))
                if existing is None or not self._same_scope(
                    existing,
                    user_id=selected_user,
                    session_id=selected_session,
                    workspace_ref=workspace_ref,
                ):
                    raise WorkspaceGoalConflict("workspace Goal replay target is unavailable")
                if (
                    str(previous.get("goal_digest") or "")
                    != self._goal_digest(existing)
                ):
                    raise WorkspaceGoalConflict(
                        "workspace Goal replay target has changed"
                    )
                result = self._receipt(existing, replayed=True, state_revision=int(state.get("_state_revision") or 0))
                return state

            # These predicates authorize fresh admission.  Check them after
            # exact replay but inside the shared writer fence, so a
            # session/project rebind cannot race the Goal append.
            current_workspace = self._current_workspace(workspace)
            if self._workspace_ref(current_workspace) != workspace_ref:
                raise WorkspaceGoalConflict("workspace changed during Goal creation")
            self._require_registered_session(
                user_id=selected_user,
                session_id=selected_session,
            )

            current_revision = int(state.get("_state_revision") or 0)
            if current_revision < 1:
                raise WorkspaceGoalConflict("Goal state revision is invalid")
            if current_revision != expected_state_revision:
                raise WorkspaceGoalConflict("workspace Goal CAS revision mismatch")

            active = [
                item
                for item in goals
                if self._is_control_goal(item)
                and str(item.get("status") or "") == "active"
                and self._same_scope(
                    item,
                    user_id=selected_user,
                    session_id=selected_session,
                    workspace_ref=workspace_ref,
                )
            ]
            if active:
                raise WorkspaceGoalConflict(
                    "one active workspace Goal already exists for this owner session"
                )
            if self._goal_by_id(goals, goal_id) is not None:
                raise WorkspaceGoalConflict("workspace Goal identity already exists")
            controlled_goals = [
                item for item in goals if self._is_control_goal(item)
            ]
            if len(controlled_goals) >= MAX_WORKSPACE_GOALS:
                raise WorkspaceGoalConflict("workspace Goal capacity exhausted")

            goal: dict[str, Any] = {
                "schema_version": GOAL_SCHEMA_VERSION,
                "goal_id": goal_id,
                "kind": KIND,
                "source": SOURCE,
                "status": "active",
                "title": selected_title,
                "description": selected_description,
                "user_id": selected_user,
                "session_id": selected_session,
                "workspace_ref": workspace_ref,
                "scope": {
                    "user_id": selected_user,
                    "session_id": selected_session,
                    "workspace_id": workspace_ref,
                },
                "priority": selected_priority,
            }
            goal["revision"] = "wgoalrev_" + self._goal_digest(goal)[:24]
            now = utc_now_iso()
            goal.update(
                {
                    "created_at": now,
                    "updated_at": now,
                }
            )
            goals.append(goal)
            operations[operation_key] = {
                "schema_version": "veyra.workspace_goal.operation.v1",
                "operation_id": operation,
                "semantic_digest": semantic_digest,
                "goal_id": goal_id,
                "goal_digest": self._goal_digest(goal),
                "user_id": selected_user,
                "session_id": selected_session,
                "created_at": now,
            }
            if len(operations) > MAX_OPERATIONS:
                operations = dict(list(operations.items())[-MAX_OPERATIONS:])
            retained_goals = retain_shared_goals(
                goals,
                touched_goal_id=goal_id,
            )
            state["goals"] = retained_goals
            state["workspace_goal_operations"] = operations
            result = self._receipt(
                goal,
                replayed=False,
                state_revision=current_revision + 1,
            )
            return state

        persisted = self.state_store.mutate_json("user_goals.json", mutate)
        if not result:
            raise WorkspaceGoalConflict("workspace Goal result was not produced")
        result["state_revision"] = int(persisted.get("_state_revision") or result["state_revision"])
        return result

    def list_scope(
        self,
        *,
        control_token: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        self._authorize(control_token)
        selected_user = normalize_scope_component(user_id, "user_id")
        selected_session = normalize_scope_component(session_id, "session_id")
        state = self.state_store.read_json("user_goals.json")
        if state.get("_state_corrupt") is True:
            raise WorkspaceGoalConflict("Goal state is corrupt")
        state_revision = state.get("_state_revision")
        if (
            isinstance(state_revision, bool)
            or not isinstance(state_revision, int)
            or state_revision < 1
        ):
            raise WorkspaceGoalConflict("Goal state revision is invalid")
        goals = state.get("goals")
        if not isinstance(goals, list):
            raise WorkspaceGoalConflict("Goal state is invalid")
        items = [
            self._public_goal(item)
            for item in goals
            if isinstance(item, dict)
            and self._is_control_goal(item)
            and str(item.get("user_id") or "") == selected_user
            and str(item.get("session_id") or "") == selected_session
        ]
        return {
            "schema_version": "veyra.workspace_goal.list.v1",
            "status": "success",
            "state_revision": state_revision,
            "count": len(items),
            "items": items,
            "authority": self.authority(),
        }

    def _authorize(self, supplied: str) -> None:
        token = str(supplied or "").strip()
        if not self.control_token or not token or not hmac.compare_digest(token, self.control_token):
            raise WorkspaceGoalUnauthorized("valid Veyra control token required")

    @staticmethod
    def _workspace(value: str) -> str:
        raw_selected = str(value or "").strip()
        if not raw_selected:
            raise WorkspaceGoalConflict("workspace is unavailable")
        try:
            selected = str(Path(raw_selected).expanduser().resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceGoalConflict("workspace is unavailable") from exc
        if not Path(selected).is_dir():
            raise WorkspaceGoalConflict("workspace is unavailable")
        return selected

    def _current_workspace(self, value: str) -> str:
        local_world = self.state_store.read_json("local_world.json")
        raw_current = str(local_world.get("current_project") or "").strip()
        if (
            local_world.get("_state_corrupt") is True
            or not raw_current
        ):
            raise WorkspaceGoalConflict("workspace is unavailable")
        try:
            selected = self._workspace(value)
            current = self._workspace(raw_current)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceGoalConflict("workspace is unavailable") from exc
        if selected != current:
            raise WorkspaceGoalConflict("workspace must match the current local project")
        return selected

    def _require_registered_session(self, *, user_id: str, session_id: str) -> None:
        channel_state = self.state_store.read_json("channel_state.json")
        if channel_state.get("_state_corrupt") is True:
            raise WorkspaceGoalConflict("channel session state is corrupt")
        sessions = channel_state.get("sessions")
        item = sessions.get(session_id) if isinstance(sessions, dict) else None
        if not isinstance(item, dict) or str(item.get("user_id") or "") != user_id:
            raise WorkspaceGoalConflict(
                "workspace Goal requires an existing exact-owner Veyra session"
            )

    @staticmethod
    def _operation(value: str) -> str:
        selected = str(value or "").strip()
        if not _OPERATION_RE.fullmatch(selected):
            raise WorkspaceGoalConflict("invalid workspace Goal operation id")
        return selected

    @staticmethod
    def _text(value: Any, field: str, *, maximum: int) -> str:
        selected = str(value or "").strip()
        if not selected or len(selected) > maximum or any(ord(char) < 32 for char in selected):
            raise WorkspaceGoalConflict(f"invalid workspace Goal {field}")
        return selected

    @classmethod
    def _optional_text(cls, value: Any, field: str, *, maximum: int) -> str | None:
        if value is None:
            return None
        return cls._text(value, field, maximum=maximum)

    @staticmethod
    def _priority(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WorkspaceGoalConflict("workspace Goal priority must be numeric")
        selected = float(value)
        if not math.isfinite(selected) or not 0.0 <= selected <= 1.0:
            raise WorkspaceGoalConflict("workspace Goal priority must be between 0 and 1")
        return selected

    @staticmethod
    def _is_control_goal(item: dict[str, Any]) -> bool:
        return (
            str(item.get("schema_version") or "") == GOAL_SCHEMA_VERSION
            and str(item.get("kind") or "") == KIND
            and str(item.get("source") or "") == SOURCE
        )

    @staticmethod
    def _same_scope(
        item: dict[str, Any],
        *,
        user_id: str,
        session_id: str,
        workspace_ref: str,
    ) -> bool:
        return (
            str(item.get("user_id") or "") == user_id
            and str(item.get("session_id") or "") == session_id
            and str(
                item.get("workspace_ref")
                or (
                    item.get("scope", {}).get("workspace_id")
                    if isinstance(item.get("scope"), dict)
                    else ""
                )
                or ""
            )
            == workspace_ref
        )

    @staticmethod
    def _workspace_ref(workspace: str) -> str:
        return "workspace:" + hashlib.sha256(workspace.encode("utf-8")).hexdigest()

    @staticmethod
    def _operation_key(*, operation_id: str, user_id: str, session_id: str) -> str:
        return _digest(
            {
                "namespace": "veyra.workspace_goal.operation.scope.v1",
                "operation_id": operation_id,
                "user_id": user_id,
                "session_id": session_id,
            }
        )

    @staticmethod
    def _goal_by_id(goals: list[dict[str, Any]], goal_id: str) -> dict[str, Any] | None:
        matches = [item for item in goals if str(item.get("goal_id") or "") == goal_id]
        if len(matches) > 1:
            raise WorkspaceGoalConflict("duplicate workspace Goal identity")
        return matches[0] if matches else None

    @staticmethod
    def _goal_digest(goal: dict[str, Any]) -> str:
        """Bind replay to the complete durable Goal, excluding store metadata."""

        return _digest(
            {
                key: value
                for key, value in goal.items()
                if key not in {"created_at", "updated_at"}
            }
        )

    @classmethod
    def _receipt(
        cls,
        goal: dict[str, Any],
        *,
        replayed: bool,
        state_revision: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": "veyra.workspace_goal.receipt.v1",
            "status": "active",
            "replayed": replayed,
            "state_revision": state_revision,
            "goal": cls._public_goal(goal),
            "authority": cls.authority(),
        }

    @staticmethod
    def _public_goal(goal: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": GOAL_SCHEMA_VERSION,
            "goal_id": goal.get("goal_id"),
            "kind": KIND,
            "status": goal.get("status"),
            "title": goal.get("title"),
            "description": goal.get("description"),
            "user_id": goal.get("user_id"),
            "session_id": goal.get("session_id"),
            "workspace_binding_present": bool(goal.get("workspace_ref")),
            "priority": goal.get("priority"),
            "revision": goal.get("revision"),
            "created_at": goal.get("created_at"),
            "updated_at": goal.get("updated_at"),
        }


__all__ = [
    "WorkspaceGoalConflict",
    "WorkspaceGoalControl",
    "WorkspaceGoalError",
    "WorkspaceGoalUnauthorized",
]
