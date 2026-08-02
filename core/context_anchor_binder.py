from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.event_schema import VeyraEvent, utc_now_iso
from interface.general_situation_contract import StructuredAnchor, stable_digest
from memory_bridge.scope import normalize_scope_component


class ContextAnchorBinder:
    """Resolve semantic turn context into non-authorizing structured anchors.

    The binder is the missing bridge between a natural-language turn and the
    event/situation graph.  A model never supplies a Goal/Commitment/Case/Task
    identifier directly.  Veyra first creates an exact-owner candidate catalog
    containing opaque, event-bound tokens; a validated semantic act may select
    only one of those tokens.

    When no durable object exists, Veyra may issue a short-lived ``context``
    thread from an already validated semantic target.  That thread is an
    epistemic association, not an Entity, fact, Goal, permission, or execution
    authority.  It can aggregate turns only inside the exact owner/session;
    GeneralSituationRuntime still requires a real active Goal or Commitment to
    cross sessions.
    """

    SCHEMA_VERSION = "veyra.context_binding.v1"
    CATALOG_SCHEMA_VERSION = "veyra.anchor_candidate_catalog.v1"
    CONTEXT_THREAD_SCHEMA_VERSION = "veyra.context_thread.v1"
    MAX_CANDIDATES = 24
    MAX_BINDINGS = 8
    MAX_THREADS_PER_TURN = 4
    THREAD_TTL_SECONDS = 24 * 60 * 60
    _GOAL_TERMINAL = {"archived", "cancelled", "closed", "completed", "expired"}
    _COMMITMENT_VISIBLE = {"active", "paused"}
    _CASE_TERMINAL = {"CANCELLED", "COMPLETED", "FAILED", "RESOLVED"}

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def candidate_index(self, event: VeyraEvent) -> dict[str, Any]:
        """Build one event-bound private index and its model-safe catalog."""

        user_id, session_id = self._owner(event)
        sources = {
            "goals": self.state_store.read_json("user_goals.json"),
            "commitments": self.state_store.read_json("user_commitments.json"),
            "tasks": self.state_store.read_json("task_state.json"),
            "cases": self.state_store.read_json("durable_case_state.json"),
            "contexts": self.state_store.read_json("context_binding_state.json"),
        }
        if any(value.get("_state_corrupt") is True for value in sources.values()):
            return {
                "status": "degraded",
                "reason": "anchor_candidate_state_corrupt",
                "event_id": event.event_id,
                "user_id": user_id,
                "session_id": session_id,
                "candidates": {},
                "model_catalog": [],
                "snapshot_digest": "",
            }

        candidates: list[dict[str, Any]] = []
        goals = sources["goals"].get("goals")
        for item in goals if isinstance(goals, list) else []:
            if not isinstance(item, dict) or str(item.get("user_id") or "") != user_id:
                continue
            status = str(item.get("status") or "active").strip().lower()
            if status in self._GOAL_TERMINAL:
                continue
            self._append_candidate(
                candidates,
                kind="goal",
                ref_id=item.get("goal_id"),
                label=item.get("title") or item.get("topic") or item.get("kind"),
                status=status,
                source_file="user_goals.json",
                source_revision=sources["goals"].get("_state_revision"),
                durable=True,
            )

        commitments = sources["commitments"].get("commitments")
        for item in commitments if isinstance(commitments, list) else []:
            if not isinstance(item, dict) or str(item.get("user_id") or "") != user_id:
                continue
            status = str(item.get("status") or "").strip().lower()
            if status not in self._COMMITMENT_VISIBLE:
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            self._append_candidate(
                candidates,
                kind="commitment",
                ref_id=item.get("commitment_id"),
                label=item.get("title") or payload.get("topic") or item.get("kind"),
                status=status,
                source_file="user_commitments.json",
                source_revision=sources["commitments"].get("_state_revision"),
                durable=True,
            )

        # Only Veyra-registered task contexts with an exact owner/session may
        # expose Task (and its already-bound Case) candidates.  Generic task
        # history is not authoritative enough to become an anchor catalog.
        task_contexts = sources["tasks"].get("agent_task_contexts")
        for task_key, item in (
            task_contexts.items() if isinstance(task_contexts, dict) else []
        ):
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("authority") or "") != "veyra_registered"
                or str(item.get("user_id") or "") != user_id
                or str(item.get("session_id") or "") != session_id
            ):
                continue
            final_status = str(item.get("final_status") or "").strip()
            if final_status and final_status not in NON_TERMINAL_STATUSES:
                continue
            task_id = item.get("runtime_task_id") or task_key
            self._append_candidate(
                candidates,
                kind="task",
                ref_id=task_id,
                label=item.get("user_goal") or item.get("route") or "active task",
                status=final_status or "active",
                source_file="task_state.json",
                source_revision=sources["tasks"].get("_state_revision"),
                durable=False,
            )
            case_id = str(item.get("case_id") or "").strip()
            if case_id and self._case_is_visible(
                case_id,
                user_id=user_id,
                task_context=item,
                cases_state=sources["cases"],
            ):
                self._append_candidate(
                    candidates,
                    kind="case",
                    ref_id=case_id,
                    label=item.get("user_goal") or "active case",
                    status="active",
                    source_file="durable_case_state.json",
                    source_revision=sources["cases"].get("_state_revision"),
                    durable=False,
                )

        now = datetime.now(timezone.utc)
        threads = sources["contexts"].get("threads")
        for item in threads.values() if isinstance(threads, dict) else []:
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("user_id") or "") != user_id
                or str(item.get("session_id") or "") != session_id
                or str(item.get("status") or "") not in {"provisional", "corroborated"}
                or self._time(item.get("expires_at")) <= now
            ):
                continue
            self._append_candidate(
                candidates,
                kind="context",
                ref_id=item.get("context_id"),
                label=item.get("label") or item.get("target_type") or "context",
                status=str(item.get("status") or "provisional"),
                source_file="context_binding_state.json",
                source_revision=sources["contexts"].get("_state_revision"),
                durable=False,
            )

        candidates = candidates[: self.MAX_CANDIDATES]
        snapshot = [
            {
                key: candidate.get(key)
                for key in (
                    "kind",
                    "ref_id",
                    "status",
                    "source_file",
                    "source_revision",
                    "durable",
                )
            }
            for candidate in candidates
        ]
        snapshot_digest = stable_digest(
            "veyra.anchor_candidate_snapshot.v1",
            {
                "event_id": event.event_id,
                "user_id": user_id,
                "session_id": session_id,
                "candidates": snapshot,
            },
        )
        private: dict[str, dict[str, Any]] = {}
        model_catalog: list[dict[str, Any]] = []
        for candidate in candidates:
            token = "actok_" + stable_digest(
                "veyra.anchor_candidate_token.v1",
                {
                    "event_id": event.event_id,
                    "user_id": user_id,
                    "session_id": session_id,
                    "snapshot_digest": snapshot_digest,
                    "kind": candidate["kind"],
                    "ref_id": candidate["ref_id"],
                },
            )[:24]
            private[token] = {**candidate, "candidate_token": token}
            model_catalog.append(
                {
                    "candidate_token": token,
                    "kind": candidate["kind"],
                    "label": candidate["label"],
                    "status": candidate["status"],
                    "durable": bool(candidate["durable"]),
                }
            )
        return {
            "status": "available",
            "schema_version": self.CATALOG_SCHEMA_VERSION,
            "event_id": event.event_id,
            "user_id": user_id,
            "session_id": session_id,
            "snapshot_digest": snapshot_digest,
            "candidates": private,
            "model_catalog": model_catalog,
        }

    def bind(
        self,
        *,
        event: VeyraEvent,
        understanding: Any,
        attention_assessment: dict[str, Any] | None,
        candidate_index: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a source-bound association envelope; never grant authority."""

        user_id, session_id = self._owner(event)
        if (
            candidate_index.get("event_id") != event.event_id
            or candidate_index.get("user_id") != user_id
            or candidate_index.get("session_id") != session_id
        ):
            return self._unresolved(event, "candidate_index_binding_mismatch")
        if candidate_index.get("status") != "available":
            return self._unresolved(
                event,
                str(candidate_index.get("reason") or "candidate_index_unavailable"),
            )
        assessment = attention_assessment if isinstance(attention_assessment, dict) else {}
        if assessment.get("status") != "assessed" or assessment.get("model_validated") is not True:
            return self._unresolved(
                event,
                str(assessment.get("reason") or "validated_semantic_assessment_required"),
            )
        frame = getattr(understanding, "semantic_frame", None)
        if frame is None or str(getattr(frame, "source", "") or "") not in {
            "model",
            "model_repair",
        }:
            return self._unresolved(event, "validated_model_semantic_frame_required")

        eligible_scores: dict[str, float] = {}
        for row in assessment.get("component_scores", []):
            if not isinstance(row, dict) or row.get("eligible") is not True:
                continue
            for act_id in row.get("act_ids", []):
                eligible_scores[str(act_id)] = float(row.get("score") or 0.0)

        private_candidates = (
            candidate_index.get("candidates")
            if isinstance(candidate_index.get("candidates"), dict)
            else {}
        )
        bindings: list[dict[str, Any]] = []
        threads: list[dict[str, Any]] = []
        used_keys: set[str] = set()
        unresolved_acts: list[dict[str, str]] = []
        for act in list(getattr(frame, "acts", []) or []):
            if len(bindings) >= self.MAX_BINDINGS:
                break
            act_id = str(getattr(act, "act_id", "") or "")
            score = eligible_scores.get(act_id)
            if score is None:
                continue
            selected = self._selected_candidate(act, private_candidates)
            if selected is not None:
                anchor = StructuredAnchor(selected["kind"], selected["ref_id"])
                if anchor.key not in used_keys:
                    used_keys.add(anchor.key)
                    bindings.append(
                        self._binding_record(
                            event=event,
                            act=act,
                            anchor=anchor,
                            score=score,
                            source="owner_scoped_candidate_selection",
                            candidate=selected,
                            snapshot_digest=str(candidate_index.get("snapshot_digest") or ""),
                        )
                    )
                continue

            thread = self._context_thread(event=event, act=act, score=score)
            if thread is None:
                unresolved_acts.append({"act_id": act_id, "reason": "stable_subject_unavailable"})
                continue
            anchor = StructuredAnchor("context", thread["context_id"])
            if anchor.key in used_keys:
                continue
            used_keys.add(anchor.key)
            threads.append(thread)
            bindings.append(
                self._binding_record(
                    event=event,
                    act=act,
                    anchor=anchor,
                    score=score,
                    source="server_issued_context_hypothesis",
                    candidate=None,
                    snapshot_digest=str(candidate_index.get("snapshot_digest") or ""),
                )
            )
            if len(threads) >= self.MAX_THREADS_PER_TURN:
                break

        anchors = sorted(
            ({"kind": item["kind"], "ref_id": item["ref_id"]} for item in bindings),
            key=lambda item: (item["kind"], item["ref_id"]),
        )
        envelope = {
            "schema_version": self.SCHEMA_VERSION,
            "event_id": event.event_id,
            "user_id": user_id,
            "session_id": session_id,
            "candidate_snapshot_digest": str(candidate_index.get("snapshot_digest") or ""),
            "semantic_frame_source": str(getattr(frame, "source", "") or ""),
            "semantic_resolver_status": str(getattr(frame, "resolver_status", "") or ""),
            "anchors": anchors,
            "bindings": bindings,
            "threads": threads,
            "unresolved_acts": unresolved_acts[:8],
            "epistemic_status": "context_hypothesis",
            "is_fact": False,
            "causality_asserted": False,
            "route_change_allowed": False,
            "risk_change_allowed": False,
            "authority": {
                "capability_grant": False,
                "tool_execution": False,
                "agent_execution": False,
                "external_delivery": False,
            },
            "created_at": utc_now_iso(),
        }
        envelope["binding_digest"] = self.binding_digest(envelope)
        envelope["operation_id"] = f"context-bind:{event.event_id}:{envelope['binding_digest'][:16]}"
        envelope["status"] = "bound" if anchors else "unresolved"
        envelope["reason"] = "anchors_resolved" if anchors else "no_eligible_anchor"
        return envelope

    @classmethod
    def binding_digest(cls, envelope: dict[str, Any]) -> str:
        stable = {
            key: copy.deepcopy(envelope.get(key))
            for key in (
                "schema_version",
                "event_id",
                "user_id",
                "session_id",
                "candidate_snapshot_digest",
                "semantic_frame_source",
                "semantic_resolver_status",
                "anchors",
                "bindings",
                "unresolved_acts",
                "epistemic_status",
                "is_fact",
                "causality_asserted",
                "route_change_allowed",
                "risk_change_allowed",
                "authority",
            )
        }
        # A context thread's identity is semantic. Wall-clock fields are
        # retention metadata and must not turn an exact model retry into a
        # conflicting binding for the same source Event.
        stable["threads"] = [
            {
                key: copy.deepcopy(thread.get(key))
                for key in (
                    "schema_version",
                    "context_id",
                    "user_id",
                    "session_id",
                    "target_type",
                    "label",
                    "subject_digest",
                    "epistemic_status",
                    "is_fact",
                    "authority",
                    "confidence",
                    "status",
                )
            }
            for thread in envelope.get("threads", [])
            if isinstance(thread, dict)
        ]
        return stable_digest("veyra.context_binding.envelope.v1", stable)

    def _context_thread(
        self,
        *,
        event: VeyraEvent,
        act: Any,
        score: float,
    ) -> dict[str, Any] | None:
        target = getattr(act, "target", None)
        referent = getattr(act, "referent", None)
        target_type = self._canonical(getattr(target, "type", ""), 120)
        target_value = self._canonical(getattr(target, "value", ""), 600)
        if not target_value and str(getattr(referent, "status", "") or "") == "resolved":
            target_value = self._canonical(getattr(referent, "resolved", ""), 600)
        if not target_type or target_type == "unknown" or not target_value:
            return None
        user_id, session_id = self._owner(event)
        subject = {
            "target_type": target_type,
            "target_value": target_value,
        }
        subject_digest = stable_digest("veyra.context_thread.subject.v1", subject)
        context_id = "ctx_" + stable_digest(
            "veyra.context_thread.identity.v1",
            {
                "user_id": user_id,
                "session_id": session_id,
                "subject_digest": subject_digest,
            },
        )[:24]
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.THREAD_TTL_SECONDS)).isoformat()
        return {
            "schema_version": self.CONTEXT_THREAD_SCHEMA_VERSION,
            "context_id": context_id,
            "user_id": user_id,
            "session_id": session_id,
            "target_type": target_type,
            "label": self._label(target_value),
            "subject_digest": subject_digest,
            "epistemic_status": "context_hypothesis",
            "is_fact": False,
            "authority": False,
            "confidence": round(float(score), 6),
            "status": "provisional",
            "source_event_ids": [event.event_id],
            "expires_at": expires_at,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }

    def _binding_record(
        self,
        *,
        event: VeyraEvent,
        act: Any,
        anchor: StructuredAnchor,
        score: float,
        source: str,
        candidate: dict[str, Any] | None,
        snapshot_digest: str,
    ) -> dict[str, Any]:
        quote = getattr(act, "source_quote", None)
        quote_text = str(getattr(quote, "text", "") or "")
        return {
            "kind": anchor.kind,
            "ref_id": anchor.ref_id,
            "act_id": str(getattr(act, "act_id", "") or ""),
            "relation": "about",
            "source": source,
            "semantic_score": round(float(score), 6),
            "source_quote": {
                "start": int(getattr(quote, "start", 0) or 0),
                "end": int(getattr(quote, "end", 0) or 0),
                "digest": hashlib.sha256(quote_text.encode("utf-8")).hexdigest(),
            },
            "candidate_snapshot_digest": snapshot_digest,
            "candidate_source": (
                {
                    "file": candidate.get("source_file"),
                    "revision": candidate.get("source_revision"),
                    "candidate_token": candidate.get("candidate_token"),
                }
                if candidate is not None
                else None
            ),
            "epistemic_status": "context_hypothesis",
            "is_fact": False,
            "causality_asserted": False,
            "authority": False,
            "source_event_id": event.event_id,
        }

    @classmethod
    def _selected_candidate(
        cls,
        act: Any,
        candidates: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        target = getattr(act, "target", None)
        attributes = getattr(target, "attributes", None)
        if not isinstance(attributes, dict):
            return None
        raw = attributes.get("anchor_candidate_token")
        if not isinstance(raw, str) or not raw:
            return None
        selected = candidates.get(raw)
        if not isinstance(selected, dict):
            return None

        # The opaque token proves only that the candidate came from this
        # event-bound catalog. It does not prove the model resolved the text to
        # that candidate. Require the act to copy the exact visible label and
        # reject duplicate labels of the same kind; otherwise the safe outcome
        # is a provisional context hypothesis, never a durable-object bind.
        target_kind = cls._canonical(getattr(target, "type", ""), 120)
        target_value = cls._canonical(getattr(target, "value", ""), 600)
        selected_kind = cls._canonical(selected.get("kind"), 120)
        selected_label = cls._canonical(selected.get("label"), 600)
        if (
            not target_value
            or target_kind != selected_kind
            or target_value != selected_label
        ):
            return None
        same_label = [
            candidate
            for candidate in candidates.values()
            if isinstance(candidate, dict)
            and cls._canonical(candidate.get("label"), 600) == selected_label
        ]
        if len(same_label) != 1:
            return None
        if selected_kind != "context":
            quote = getattr(act, "source_quote", None)
            quote_text = cls._canonical(getattr(quote, "text", ""), 800)
            if not selected_label or selected_label not in quote_text:
                return None
        return copy.deepcopy(selected)

    @staticmethod
    def _append_candidate(
        output: list[dict[str, Any]],
        *,
        kind: str,
        ref_id: Any,
        label: Any,
        status: str,
        source_file: str,
        source_revision: Any,
        durable: bool,
    ) -> None:
        try:
            anchor = StructuredAnchor(kind, str(ref_id or ""))
        except ValueError:
            return
        if any(item.get("kind") == anchor.kind and item.get("ref_id") == anchor.ref_id for item in output):
            return
        safe_label = redact_sensitive(str(label or kind), max_string=180, max_list=2)
        output.append(
            {
                "kind": anchor.kind,
                "ref_id": anchor.ref_id,
                "label": str(safe_label or kind)[:180],
                "status": str(status or "unknown")[:80],
                "source_file": source_file,
                "source_revision": int(source_revision or 0),
                "durable": bool(durable),
            }
        )

    @classmethod
    def _case_is_visible(
        cls,
        case_id: str,
        *,
        user_id: str,
        task_context: dict[str, Any],
        cases_state: dict[str, Any],
    ) -> bool:
        cases = cases_state.get("cases")
        case = cases.get(case_id) if isinstance(cases, dict) else None
        if not isinstance(case, dict):
            return False
        scope = case.get("scope") if isinstance(case.get("scope"), dict) else {}
        return bool(
            str(scope.get("user_id") or "") == user_id
            and str(scope.get("workspace_id") or "")
            == str(task_context.get("case_workspace_id") or "")
            and str(case.get("status") or "") not in cls._CASE_TERMINAL
        )

    @staticmethod
    def _owner(event: VeyraEvent) -> tuple[str, str]:
        return (
            normalize_scope_component(event.source.user_id, "user_id"),
            normalize_scope_component(event.source.session_id, "session_id"),
        )

    @staticmethod
    def _canonical(value: Any, limit: int) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        return " ".join(text.split()).casefold()[:limit]

    @staticmethod
    def _label(value: Any) -> str:
        safe = redact_sensitive(str(value or ""), max_string=180, max_list=2)
        return str(safe or "context")[:180]

    @staticmethod
    def _time(value: Any) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    @classmethod
    def _unresolved(cls, event: VeyraEvent, reason: str) -> dict[str, Any]:
        user_id, session_id = cls._owner(event)
        envelope = {
            "schema_version": cls.SCHEMA_VERSION,
            "status": "unresolved",
            "reason": str(reason or "unresolved")[:160],
            "event_id": event.event_id,
            "user_id": user_id,
            "session_id": session_id,
            "candidate_snapshot_digest": "",
            "semantic_frame_source": "",
            "semantic_resolver_status": "",
            "anchors": [],
            "bindings": [],
            "threads": [],
            "unresolved_acts": [],
            "epistemic_status": "unknown",
            "is_fact": False,
            "causality_asserted": False,
            "route_change_allowed": False,
            "risk_change_allowed": False,
            "authority": {
                "capability_grant": False,
                "tool_execution": False,
                "agent_execution": False,
                "external_delivery": False,
            },
            "created_at": utc_now_iso(),
        }
        envelope["binding_digest"] = cls.binding_digest(envelope)
        envelope["operation_id"] = f"context-bind:{event.event_id}:{envelope['binding_digest'][:16]}"
        return envelope
