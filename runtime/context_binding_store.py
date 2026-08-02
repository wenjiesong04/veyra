from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from core.context_anchor_binder import ContextAnchorBinder
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from interface.general_situation_contract import StructuredAnchor
from memory_bridge.scope import normalize_scope_component


class ContextBindingConflictError(ValueError):
    """One source event was rebound to different context semantics."""


class ContextBindingStore:
    """Private, owner-scoped sidecar for post-Understanding event bindings."""

    STATE_FILE = "context_binding_state.json"
    SCHEMA_VERSION = "veyra.context_binding_state.v1"
    MAX_BINDINGS = 2000
    MAX_THREADS = 1000

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def register(self, envelope: dict[str, Any]) -> dict[str, Any]:
        selected = self._validated_envelope(envelope)
        event_id = selected["event_id"]
        binding_digest = selected["binding_digest"]
        result: dict[str, Any] = {}

        def mutate(state: dict[str, Any]) -> None:
            nonlocal result
            self._require_healthy(state)
            bindings = (
                copy.deepcopy(state.get("bindings"))
                if isinstance(state.get("bindings"), dict)
                else {}
            )
            threads = (
                copy.deepcopy(state.get("threads"))
                if isinstance(state.get("threads"), dict)
                else {}
            )
            existing = bindings.get(event_id)
            if isinstance(existing, dict):
                self._assert_owner(existing, selected)
                if str(existing.get("binding_digest") or "") != binding_digest:
                    raise ContextBindingConflictError(
                        "source event is already bound to different context semantics"
                    )
                result = {
                    "status": "replayed",
                    "event_id": event_id,
                    "binding_digest": binding_digest,
                    "application_status": existing.get("application_status"),
                }
                return

            now = utc_now_iso()
            record = {
                **copy.deepcopy(selected),
                "resolution_status": selected["status"],
                "application_status": (
                    "pending" if selected["status"] == "bound" else "not_applicable"
                ),
                "registered_at": now,
                "updated_at": now,
            }
            bindings[event_id] = record
            for thread in selected.get("threads", []):
                self._merge_thread(threads, thread, event_id=event_id, now=now)
            bindings = self._bounded_bindings(bindings)
            threads = self._bounded_threads(threads)
            state.update(
                {
                    "schema_version": self.SCHEMA_VERSION,
                    "bindings": bindings,
                    "threads": threads,
                    "binding_count": len(bindings),
                    "thread_count": len(threads),
                    "coverage": self._coverage(bindings),
                    "updated_at": now,
                }
            )
            result = {
                "status": "registered",
                "event_id": event_id,
                "binding_digest": binding_digest,
                "application_status": record["application_status"],
            }

        self.state_store.mutate_json(self.STATE_FILE, mutate)
        return result

    def get(
        self,
        event_id: str,
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        selected_event = str(event_id or "").strip()
        selected_user = normalize_scope_component(user_id, "user_id")
        selected_session = normalize_scope_component(session_id, "session_id")
        state = self.state_store.read_json(self.STATE_FILE)
        if state.get("_state_corrupt") is True:
            return None
        bindings = state.get("bindings") if isinstance(state.get("bindings"), dict) else {}
        record = bindings.get(selected_event)
        if not isinstance(record, dict):
            return None
        if (
            str(record.get("user_id") or "") != selected_user
            or str(record.get("session_id") or "") != selected_session
        ):
            return None
        return copy.deepcopy(record)

    def pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        state = self.state_store.read_json(self.STATE_FILE)
        if state.get("_state_corrupt") is True:
            return []
        bindings = state.get("bindings") if isinstance(state.get("bindings"), dict) else {}
        selected = [
            copy.deepcopy(item)
            for item in bindings.values()
            if isinstance(item, dict)
            and item.get("resolution_status") == "bound"
            and item.get("application_status") == "pending"
        ]
        selected.sort(key=lambda item: str(item.get("registered_at") or ""))
        return selected[: max(0, min(int(limit), 1000))]

    def mark_applied(
        self,
        *,
        event_id: str,
        binding_digest: str,
        situation_id: str,
        observation_revision: int,
    ) -> dict[str, Any]:
        selected_event = str(event_id or "").strip()
        selected_digest = str(binding_digest or "").strip()
        selected_situation = str(situation_id or "").strip()
        if not selected_event or not selected_digest or not selected_situation:
            raise ValueError("context binding application identity is incomplete")
        if isinstance(observation_revision, bool) or not isinstance(observation_revision, int) or observation_revision < 1:
            raise ValueError("context binding observation_revision is invalid")
        result: dict[str, Any] = {}

        def mutate(state: dict[str, Any]) -> None:
            nonlocal result
            self._require_healthy(state)
            bindings = state.get("bindings") if isinstance(state.get("bindings"), dict) else {}
            record = bindings.get(selected_event)
            if not isinstance(record, dict):
                raise KeyError("context binding is missing")
            if str(record.get("binding_digest") or "") != selected_digest:
                raise ContextBindingConflictError("context binding digest changed before apply")
            existing_situation = str(record.get("situation_id") or "")
            if existing_situation and existing_situation != selected_situation:
                raise ContextBindingConflictError("context binding is already applied to another Situation")
            existing_revision = int(record.get("applied_observation_revision") or 0)
            if (
                record.get("application_status") == "applied"
                and existing_situation == selected_situation
            ):
                result = {
                    "status": "replayed",
                    "event_id": selected_event,
                    "situation_id": selected_situation,
                    "observation_revision": existing_revision,
                }
                return
            if existing_revision > observation_revision:
                raise ContextBindingConflictError(
                    "context binding application revision moved backwards"
                )
            record.update(
                {
                    "application_status": "applied",
                    "situation_id": selected_situation,
                    "applied_observation_revision": observation_revision,
                    "applied_at": record.get("applied_at") or utc_now_iso(),
                    "updated_at": utc_now_iso(),
                }
            )
            state["updated_at"] = utc_now_iso()
            result = {
                "status": "applied",
                "event_id": selected_event,
                "situation_id": selected_situation,
                "observation_revision": observation_revision,
            }

        self.state_store.mutate_json(self.STATE_FILE, mutate)
        return result

    def status(self) -> dict[str, Any]:
        state = self.state_store.read_json(self.STATE_FILE)
        if state.get("_state_corrupt") is True:
            return {"status": "degraded", "reason": "context_binding_state_corrupt"}
        bindings = state.get("bindings") if isinstance(state.get("bindings"), dict) else {}
        return {
            "status": "available",
            "binding_count": len(bindings),
            "thread_count": len(state.get("threads") or {}),
            "pending_count": sum(
                1
                for item in bindings.values()
                if isinstance(item, dict) and item.get("application_status") == "pending"
            ),
            "coverage": copy.deepcopy(state.get("coverage") or {}),
            "updated_at": state.get("updated_at"),
            "authority": {
                "route_change": False,
                "risk_change": False,
                "capability_grant": False,
                "execution": False,
                "external_delivery": False,
            },
        }

    @classmethod
    def _validated_envelope(cls, envelope: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(envelope, dict):
            raise TypeError("context binding envelope must be a mapping")
        selected = copy.deepcopy(envelope)
        if selected.get("schema_version") != ContextAnchorBinder.SCHEMA_VERSION:
            raise ValueError("context binding schema is invalid")
        selected["event_id"] = str(selected.get("event_id") or "").strip()
        if not selected["event_id"] or len(selected["event_id"]) > 240:
            raise ValueError("context binding event_id is invalid")
        selected["user_id"] = normalize_scope_component(selected.get("user_id"), "user_id")
        selected["session_id"] = normalize_scope_component(selected.get("session_id"), "session_id")
        if selected.get("status") not in {"bound", "unresolved"}:
            raise ValueError("context binding resolution status is invalid")
        if any(
            selected.get(key) is not expected
            for key, expected in (
                ("is_fact", False),
                ("causality_asserted", False),
                ("route_change_allowed", False),
                ("risk_change_allowed", False),
            )
        ):
            raise ValueError("context binding cannot carry fact or policy authority")
        authority = selected.get("authority") if isinstance(selected.get("authority"), dict) else {}
        if not authority or any(bool(value) for value in authority.values()):
            raise ValueError("context binding authority must be explicitly empty")
        anchors = selected.get("anchors")
        bindings = selected.get("bindings")
        threads = selected.get("threads")
        if not isinstance(anchors, list) or not isinstance(bindings, list) or not isinstance(threads, list):
            raise ValueError("context binding collections are invalid")
        normalized_anchors = [StructuredAnchor.from_dict(item).to_dict() for item in anchors]
        if (selected["status"] == "bound") != bool(normalized_anchors):
            raise ValueError(
                "context binding status does not match its anchor collection"
            )
        if len(normalized_anchors) > ContextAnchorBinder.MAX_BINDINGS:
            raise ValueError("context binding anchor capacity exceeded")
        anchor_keys = {f"{item['kind']}:{item['ref_id']}" for item in normalized_anchors}
        for item in bindings:
            if not isinstance(item, dict):
                raise ValueError("context binding provenance is invalid")
            anchor = StructuredAnchor(str(item.get("kind") or ""), str(item.get("ref_id") or ""))
            if anchor.key not in anchor_keys:
                raise ValueError("context binding provenance references an unknown anchor")
            if (
                item.get("is_fact") is not False
                or item.get("causality_asserted") is not False
                or item.get("authority") is not False
                or str(item.get("source_event_id") or "") != selected["event_id"]
            ):
                raise ValueError("context binding provenance weakens the epistemic boundary")
            source_quote = item.get("source_quote") if isinstance(item.get("source_quote"), dict) else {}
            digest = str(source_quote.get("digest") or "")
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("context binding source quote digest is invalid")
        for thread in threads:
            cls._validate_thread(thread, selected)
        expected_digest = ContextAnchorBinder.binding_digest(selected)
        if str(selected.get("binding_digest") or "") != expected_digest:
            raise ValueError("context binding digest mismatch")
        expected_operation = (
            f"context-bind:{selected['event_id']}:{expected_digest[:16]}"
        )
        if str(selected.get("operation_id") or "") != expected_operation:
            raise ValueError("context binding operation identity is invalid")
        selected["anchors"] = normalized_anchors
        return selected

    @classmethod
    def _validate_thread(cls, thread: Any, envelope: dict[str, Any]) -> None:
        if not isinstance(thread, dict) or thread.get("schema_version") != ContextAnchorBinder.CONTEXT_THREAD_SCHEMA_VERSION:
            raise ValueError("context thread schema is invalid")
        anchor = StructuredAnchor("context", str(thread.get("context_id") or ""))
        if not any(
            item.get("kind") == anchor.kind and item.get("ref_id") == anchor.ref_id
            for item in envelope.get("anchors", [])
            if isinstance(item, dict)
        ):
            raise ValueError("context thread lacks a matching anchor")
        if (
            str(thread.get("user_id") or "") != envelope["user_id"]
            or str(thread.get("session_id") or "") != envelope["session_id"]
            or thread.get("is_fact") is not False
            or thread.get("authority") is not False
            or str(thread.get("epistemic_status") or "") != "context_hypothesis"
        ):
            raise ValueError("context thread owner or authority boundary is invalid")
        digest = str(thread.get("subject_digest") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("context thread subject digest is invalid")

    @classmethod
    def _merge_thread(
        cls,
        threads: dict[str, dict[str, Any]],
        incoming: dict[str, Any],
        *,
        event_id: str,
        now: str,
    ) -> None:
        context_id = str(incoming.get("context_id") or "")
        existing = threads.get(context_id)
        if isinstance(existing, dict):
            if any(
                str(existing.get(key) or "") != str(incoming.get(key) or "")
                for key in ("user_id", "session_id", "subject_digest")
            ):
                raise ContextBindingConflictError("context thread identity collision")
            source_events = sorted(
                {
                    str(item)
                    for item in [*(existing.get("source_event_ids") or []), event_id]
                    if str(item)
                }
            )
            existing.update(
                {
                    "source_event_ids": source_events[-64:],
                    "status": "corroborated" if len(source_events) >= 2 else "provisional",
                    "confidence": max(float(existing.get("confidence") or 0.0), float(incoming.get("confidence") or 0.0)),
                    "expires_at": max(str(existing.get("expires_at") or ""), str(incoming.get("expires_at") or "")),
                    "updated_at": now,
                }
            )
            return
        threads[context_id] = {
            **copy.deepcopy(incoming),
            "source_event_ids": [event_id],
            "status": "provisional",
            "created_at": incoming.get("created_at") or now,
            "updated_at": now,
        }

    @classmethod
    def _bounded_bindings(cls, bindings: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if len(bindings) <= cls.MAX_BINDINGS:
            return bindings
        pending = {
            key: value
            for key, value in bindings.items()
            if isinstance(value, dict) and value.get("application_status") == "pending"
        }
        if len(pending) > cls.MAX_BINDINGS:
            raise RuntimeError("context binding pending capacity exhausted")
        applied = sorted(
            (
                (key, value)
                for key, value in bindings.items()
                if key not in pending and isinstance(value, dict)
            ),
            key=lambda item: str(item[1].get("updated_at") or item[1].get("registered_at") or ""),
            reverse=True,
        )
        keep = dict(applied[: cls.MAX_BINDINGS - len(pending)])
        keep.update(pending)
        return keep

    @classmethod
    def _bounded_threads(cls, threads: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if len(threads) <= cls.MAX_THREADS:
            return threads
        now = datetime.now(timezone.utc)
        active: list[tuple[str, dict[str, Any]]] = []
        expired: list[tuple[str, dict[str, Any]]] = []
        for key, value in threads.items():
            target = active if cls._time(value.get("expires_at")) > now else expired
            target.append((key, value))
        active.sort(key=lambda item: str(item[1].get("updated_at") or ""), reverse=True)
        expired.sort(key=lambda item: str(item[1].get("updated_at") or ""), reverse=True)
        return dict((active + expired)[: cls.MAX_THREADS])

    @staticmethod
    def _coverage(bindings: dict[str, dict[str, Any]]) -> dict[str, Any]:
        eligible = len(bindings)
        bound = sum(1 for item in bindings.values() if isinstance(item, dict) and item.get("resolution_status") == "bound")
        unresolved = max(0, eligible - bound)
        return {
            "eligible": eligible,
            "bound": bound,
            "unresolved": unresolved,
            "bound_rate": round(bound / eligible, 6) if eligible else 0.0,
            "overconservative_alert": bool(eligible >= 5 and bound == 0),
        }

    @staticmethod
    def _assert_owner(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
        if any(
            str(existing.get(key) or "") != str(incoming.get(key) or "")
            for key in ("event_id", "user_id", "session_id")
        ):
            raise ContextBindingConflictError("context binding owner changed")

    @classmethod
    def _require_healthy(cls, state: dict[str, Any]) -> None:
        if state.get("_state_corrupt") is True:
            raise ValueError("context binding state is corrupt")
        schema = str(state.get("schema_version") or cls.SCHEMA_VERSION)
        if schema != cls.SCHEMA_VERSION:
            raise ValueError("context binding state schema is invalid")

    @staticmethod
    def _time(value: Any) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)
