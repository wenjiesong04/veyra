"""Server-owned semantic Situation CRUD over the single state repository."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

from core.situation_state_repository import SituationStateRepository
from core.world_state import StateRevisionConflictError, WorldStateStore
from interface.event_schema import VeyraEvent


class SemanticSituationRuntime:
    """CAS/replay writer for semantic Situations; never grants authority."""

    TERMINAL_STATUSES = frozenset({"resolved", "expired", "contradicted", "archived"})

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        repository: SituationStateRepository | None = None,
    ) -> None:
        self.state_store = state_store
        self.repository = repository or SituationStateRepository(state_store)

    def list_semantic(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
        newest_first: bool = True,
    ) -> list[dict[str, Any]]:
        return self.repository.list(
            user_id=user_id,
            session_id=session_id,
            limit=limit,
            newest_first=newest_first,
            semantic_only=True,
        )

    def get_semantic(
        self,
        situation_id: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self.repository.get(
            situation_id,
            user_id=user_id,
            session_id=session_id,
            semantic_only=True,
        )

    def semantic_situation_id(self, *, user_id: str, subject_key: str) -> str:
        return self.repository.stable_situation_id(user_id=user_id, subject_key=subject_key)

    def record_semantic(
        self,
        event: VeyraEvent,
        *,
        subject_key: str,
        semantic_state: dict[str, Any],
        situation_id: str | None = None,
        operation: str = "update",
        expected_revision: int | None = None,
        evidence_refs: Iterable[Any] | Any | None = None,
        reopen: bool = False,
        direct_user_asserted: bool = False,
        source_quote_valid: bool = False,
        server_command: bool = False,
    ) -> dict[str, Any]:
        selected_operation = str(operation or "").strip().lower()
        if selected_operation not in {"create", "update", "correct", "resolve"}:
            raise ValueError("semantic operation is invalid")
        subject = str(subject_key or "")[:240]
        if not subject:
            raise ValueError("semantic subject_key is required")
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1
        ):
            raise ValueError("semantic expected_revision is invalid")
        if not isinstance(reopen, bool):
            raise ValueError("semantic reopen is invalid")
        if reopen and selected_operation != "correct":
            raise ValueError("semantic reopen requires correct operation")
        normalized = self.repository.normalize_semantic(semantic_state)
        reason = str(semantic_state.get("reopen_reason") or "")[:240]
        if reopen and not reason:
            raise ValueError("semantic reopen requires a reason")
        if reopen and normalized["lifecycle"] in self.TERMINAL_STATUSES:
            raise ValueError("semantic reopen must target a non-terminal lifecycle")
        if selected_operation == "resolve" and normalized["lifecycle"] != "resolved":
            raise ValueError("resolve operation must persist resolved lifecycle")
        if selected_operation in {"correct", "resolve"} and not (
            server_command or (direct_user_asserted and source_quote_valid)
        ):
            raise ValueError("lifecycle mutation requires a direct quoted user assertion or server command")
        source = self._event_source(event)
        observation = self._observation_provenance(event)
        user_id = source["user_id"]
        session_id = source["session_id"]
        selected_id = str(situation_id or "")[:240]
        if selected_operation == "create" and selected_id:
            raise ValueError("create cannot target an existing Situation")
        if selected_operation != "create" and not selected_id:
            raise ValueError("semantic update requires a Situation token")
        if not selected_id:
            selected_id = self.semantic_situation_id(user_id=user_id, subject_key=subject)
        evidence = self._evidence_refs(evidence_refs)
        event_id = source["event_id"]
        fingerprint = self._digest(
            {
                "event_id": event_id,
                "operation": selected_operation,
                "subject_key": subject,
                "semantic": normalized,
                "evidence": evidence,
                "reopen": reopen,
                "reopen_reason": reason,
            }
        )
        now = source["timestamp"]
        persisted: dict[str, Any] | None = None
        replayed = False

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal persisted, replayed
            rows = state["situations"]
            index = next((i for i, row in enumerate(rows) if str(row.get("situation_id") or "") == selected_id), None)
            existing = rows[index] if index is not None else None
            if existing is not None:
                if str(existing.get("record_kind") or "") != "semantic_situation":
                    raise ValueError("Situation token is not semantic")
                self._assert_scope(existing, user_id, session_id)
                prior = next(
                    (item for item in existing.get("semantic_event_fingerprints", []) if isinstance(item, dict) and item.get("event_id") == event_id),
                    None,
                )
                if prior is not None:
                    if str(prior.get("fingerprint") or "") != fingerprint:
                        raise ValueError("semantic event replay has different content")
                    persisted = copy.deepcopy(existing)
                    persisted["semantic_replayed"] = True
                    replayed = True
                    return state
                if selected_operation == "create":
                    raise StateRevisionConflictError("semantic Situation already exists")
                current_revision = int(existing.get("observation_revision") or 0)
                if expected_revision is not None and current_revision != expected_revision:
                    raise StateRevisionConflictError("semantic Situation revision mismatch")
                current_status = str(existing.get("status") or (existing.get("semantic") or {}).get("lifecycle") or "active")
                next_status = normalized["lifecycle"]
                if current_status in self.TERMINAL_STATUSES and next_status not in self.TERMINAL_STATUSES and not (selected_operation == "correct" and reopen):
                    raise StateRevisionConflictError("terminal Situation requires correct+reopen")
                target = copy.deepcopy(existing)
                target.update({
                    "semantic": copy.deepcopy(normalized),
                    "status": next_status,
                    "source_event": copy.deepcopy(source),
                    "source_event_id": event_id,
                    "source_event_type": source["type"],
                    "correlation_id": source["correlation_id"],
                    "evidence_refs": self._merge_evidence(target.get("evidence_refs"), evidence),
                    "observation_revision": current_revision + 1,
                    "source_observation_fingerprint": fingerprint,
                    "updated_at": now,
                    "reopen": reopen,
                    "reopen_reason": reason,
                })
                target["semantic_history"] = self._append(
                    target.get("semantic_history"),
                    {"event_id": event_id, "operation": selected_operation, "revision": target["observation_revision"], "material_change": normalized.get("material_change", ""), "reopen": reopen, "reopen_reason": reason, "recorded_at": now},
                )
                target["semantic_event_fingerprints"] = self._append(
                    target.get("semantic_event_fingerprints"), {"event_id": event_id, "fingerprint": fingerprint}
                )
                target["observations"] = self._append(
                    target.get("observations"), {**observation, "source_event_id": event_id, "observation_id": event_id, "recorded_at": now}
                )
                rows[index] = target
                persisted = copy.deepcopy(target)
            else:
                if selected_operation != "create":
                    raise KeyError(f"unknown semantic Situation: {selected_id}")
                if expected_revision is not None:
                    raise StateRevisionConflictError("create cannot specify expected_revision")
                target = {
                    "record_kind": "semantic_situation", "situation_id": selected_id, "semantic_subject_key": subject,
                    "correlation_id": source["correlation_id"], "user_id": user_id, "session_id": session_id, "channel": source["channel"],
                    "source_event": copy.deepcopy(source), "source_event_id": event_id, "source_event_type": source["type"],
                    "goal_refs": [], "commitment_refs": [], "structured_anchor_refs": [], "evidence_refs": evidence,
                    "salience_components": {}, "salience_score": 0.0, "status": normalized["lifecycle"], "semantic": copy.deepcopy(normalized),
                    "reopen": reopen, "reopen_reason": reason,
                    "observations": [{**observation, "source_event_id": event_id, "observation_id": event_id, "recorded_at": now}],
                    "inferences": [], "decision": None, "prediction": None, "outcome": None, "decision_history": [], "prediction_history": [], "outcome_history": [],
                    "semantic_history": [{"event_id": event_id, "operation": "create", "revision": 1, "material_change": normalized.get("material_change", ""), "recorded_at": now}],
                    "semantic_event_fingerprints": [{"event_id": event_id, "fingerprint": fingerprint}],
                    "next_evaluation_at": normalized.get("next_observation_at") or now, "created_at": now, "updated_at": now,
                    "observation_revision": 1, "source_observation_fingerprint": fingerprint,
                }
                rows.append(target)
                persisted = copy.deepcopy(target)
            state["updated_at"] = now
            state["count"] = len(rows)
            return state

        self.repository.mutate(mutate)
        if persisted is None:
            raise RuntimeError("semantic Situation was not persisted")
        if replayed:
            return persisted
        return {**copy.deepcopy(persisted), "semantic_replayed": False}

    @staticmethod
    def _assert_scope(row: dict[str, Any], user_id: str, session_id: str) -> None:
        if str(row.get("user_id") or "") != user_id or str(row.get("session_id") or "") != session_id:
            raise PermissionError("Situation belongs to a different owner/session")

    @staticmethod
    def _observation_provenance(event: VeyraEvent) -> dict[str, Any]:
        """Keep source receipts distinct from user-reported observations."""

        payload = event.payload if isinstance(event.payload, dict) else {}
        source = str(payload.get("source") or "").strip().lower()
        if event.type.value == "observation" and payload.get("source_receipt_id") and source in {
            "calendar",
            "weather",
            "public_web",
            "user_answer",
        }:
            return {
                "source": f"source:{source}",
                "source_kind": source,
                "epistemic_status": "inferred",
                "is_fact": False,
            }
        return {"source": "user_message", "epistemic_status": "reported", "is_fact": False}

    @staticmethod
    def _append(rows: Any, value: dict[str, Any], limit: int = 24) -> list[dict[str, Any]]:
        output = [copy.deepcopy(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
        output.append(copy.deepcopy(value))
        return output[-limit:]

    @staticmethod
    def _evidence_refs(refs: Iterable[Any] | Any | None) -> list[dict[str, Any]]:
        if refs is None:
            return []
        values = [refs] if isinstance(refs, (str, bytes, dict)) else list(refs)
        output: list[dict[str, Any]] = []
        for item in values[:64]:
            if isinstance(item, dict):
                output.append({str(key)[:120]: str(value)[:1000] for key, value in list(item.items())[:16]})
            elif str(item).strip():
                output.append({"ref_id": str(item)[:1000]})
        return output

    @staticmethod
    def _merge_evidence(existing: Any, incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
        values = list(existing) if isinstance(existing, list) else []
        seen = {json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) for item in values if isinstance(item, dict)}
        for item in incoming:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                values.append(copy.deepcopy(item)); seen.add(key)
        return values[-64:]

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()

    @staticmethod
    def _event_source(event: VeyraEvent) -> dict[str, Any]:
        source = event.source
        event_type = event.type.value if isinstance(event.type, Enum) else str(event.type)
        timestamp = str(event.timestamp or datetime.now(timezone.utc).isoformat())
        return {
            "event_id": str(event.event_id)[:240], "type": event_type[:120], "timestamp": timestamp,
            "occurred_at": str(getattr(event, "occurred_at", None) or timestamp)[:80], "received_at": str(getattr(event, "received_at", None) or timestamp)[:80],
            "correlation_id": str(getattr(event, "correlation_id", None) or event.event_id)[:240], "causation_id": str(getattr(event, "causation_id", None) or "")[:240] or None,
            "channel": str(getattr(source, "channel", "") or "unknown")[:120], "user_id": str(getattr(source, "user_id", "") or "local-user")[:240], "session_id": str(getattr(source, "session_id", "") or "local-session")[:240],
            "event_observed": True, "payload_claims_verified": False,
        }
