from __future__ import annotations

import copy
import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.state_compact import compact_foresight, compact_guardian_decision, compact_review_item
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


_GOVERNANCE_ONLY_EXECUTION_STATUSES = {
    "approved_no_op",
    "approved_noop",
    "governance_only",
}
_COMPLETED_EXECUTION_STATUSES = {
    "diagnosed",
    "executed",
    "observed",
    "ok",
    "probed_fallback",
    "recovered",
    "refreshed",
    "restored",
    "success",
    "verified_success",
}
_NOT_EXECUTED_STATUSES = {
    "blocked",
    "needs_confirmation",
    "not_available",
    "not_configured",
    "not_found",
    "not_restorable",
    "not_supported",
}
_INCOMPLETE_EXECUTION_STATUSES = {
    "needs_more_probe",
    "partial",
    "partially_success",
    "pending",
    "running",
    "submitted",
}
_FAILED_EXECUTION_STATUSES = {
    "error",
    "execution_failed",
    "failed",
    "needs_rollback",
    "timeout",
    "verified_failed",
}


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _claim_token_digest(claim_token: str) -> str:
    return hashlib.sha256(claim_token.encode("utf-8")).hexdigest()


def _normalize_execution_status(value: Any) -> str:
    return "_".join(str(value or "").strip().lower().replace("-", "_").split())


def _has_nonzero_returncode(payload: dict[str, Any]) -> bool:
    if "returncode" not in payload or payload.get("returncode") is None:
        return False
    try:
        return int(payload["returncode"]) != 0
    except (TypeError, ValueError):
        return True


def _has_explicit_failure(payload: dict[str, Any]) -> bool:
    status = _normalize_execution_status(payload.get("status"))
    return bool(
        status in _FAILED_EXECUTION_STATUSES
        or payload.get("success") is False
        or payload.get("applied") is False
        or payload.get("ok") is False
        or payload.get("failed") is True
        or payload.get("needs_rollback") is True
        or _normalize_execution_status(payload.get("next_action"))
        in {"rollback", "rollback_or_compensate"}
        or _has_nonzero_returncode(payload)
        or bool(payload.get("error"))
    )


def _has_incomplete_evidence(payload: dict[str, Any]) -> bool:
    status = _normalize_execution_status(payload.get("status"))
    return status in _NOT_EXECUTED_STATUSES | _INCOMPLETE_EXECUTION_STATUSES


def _execution_audit_status(execution_result: dict[str, Any]) -> str:
    execution_status = _normalize_execution_status(execution_result.get("status"))
    if execution_status in _GOVERNANCE_ONLY_EXECUTION_STATUSES:
        return "governance_only"
    if execution_status in _NOT_EXECUTED_STATUSES:
        return "not_executed"

    evidence = [execution_result]
    verification = execution_result.get("verification")
    if isinstance(verification, dict):
        evidence.append(verification)
    tool_result = execution_result.get("tool_result")
    if isinstance(tool_result, dict):
        evidence.append(tool_result)
        tool_verification = tool_result.get("verification")
        if isinstance(tool_verification, dict):
            evidence.append(tool_verification)
    if any(_has_explicit_failure(item) for item in evidence):
        return "execution_failed"
    if any(_has_incomplete_evidence(item) for item in evidence):
        return "execution_incomplete"

    if execution_status in _COMPLETED_EXECUTION_STATUSES:
        return "executed"
    if execution_status in _INCOMPLETE_EXECUTION_STATUSES:
        return "execution_incomplete"
    return "execution_unknown"


class ReviewQueue:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def create(
        self,
        event_id: str,
        task_text: str,
        risk_level: str,
        foresight: dict[str, Any],
        guardian_decision: dict[str, Any],
        proposal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        review = compact_review_item(
            {
                "review_id": f"rev_{uuid4().hex[:12]}",
                "event_id": event_id,
                "task_text": task_text,
                "risk_level": risk_level,
                "status": "pending",
                "foresight": compact_foresight(foresight),
                "guardian_decision": compact_guardian_decision(guardian_decision),
                "proposal": proposal,
                "execution_result": None,
                "created_at": utc_now_iso(),
                "decided_at": None,
                "decision_reason": None,
            }
        )
        def append_review(state: dict[str, Any]) -> None:
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            items.append(review)

        self.state_store.mutate_json("review_queue.json", append_review)
        self.state_store.append_jsonl("action_record.jsonl", {"event_id": event_id, "route": "human_review", "status": "pending", "artifacts": {"review": review}})
        return review

    def create_once(
        self,
        *,
        dedupe_key: str,
        event_id: str,
        task_text: str,
        risk_level: str,
        foresight: dict[str, Any],
        guardian_decision: dict[str, Any],
        proposal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically admit one semantic review across retries and crashes."""

        normalized_key = str(dedupe_key or "").strip()
        if not normalized_key:
            raise ValueError("dedupe_key is required")
        candidate = compact_review_item(
            {
                "review_id": f"rev_{uuid4().hex[:12]}",
                "dedupe_key": normalized_key,
                "event_id": event_id,
                "task_text": task_text,
                "risk_level": risk_level,
                "status": "pending",
                "foresight": compact_foresight(foresight),
                "guardian_decision": compact_guardian_decision(
                    guardian_decision
                ),
                "proposal": proposal,
                "execution_result": None,
                "created_at": utc_now_iso(),
                "decided_at": None,
                "decision_reason": None,
            }
        )
        selected: dict[str, Any] | None = None
        created = False

        def admit(state: dict[str, Any]) -> None:
            nonlocal selected, created
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("dedupe_key") == normalized_key
                ):
                    selected = dict(item)
                    return
            items.append(candidate)
            selected = dict(candidate)
            created = True

        self.state_store.mutate_json("review_queue.json", admit)
        if selected is None:
            raise RuntimeError("review admission did not produce a result")
        if created:
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "event_id": event_id,
                    "route": "human_review",
                    "status": "pending",
                    "artifacts": {"review": selected},
                },
            )
        return selected

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self.state_store.read_json("review_queue.json").get("items", [])
        if status:
            return [item for item in items if item.get("status") == status]
        return items

    def diagnostic(self, *, stale_after_days: int = 7) -> dict[str, Any]:
        items = [item for item in self.list() if isinstance(item, dict)]
        pending = [self._diagnostic_item(item, stale_after_days=stale_after_days) for item in items if item.get("status") == "pending"]
        by_risk: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for item in pending:
            risk = str(item.get("risk") or "unknown")
            review_type = str(item.get("type") or "unknown")
            by_risk[risk] = by_risk.get(risk, 0) + 1
            by_type[review_type] = by_type.get(review_type, 0) + 1
        stale = [item for item in pending if item.get("stale")]
        return {
            "status": "success",
            "checked_at": utc_now_iso(),
            "pending_count": len(pending),
            "stale_pending_count": len(stale),
            "by_risk": by_risk,
            "by_type": by_type,
            "pending": pending,
            "actions": {
                "mark_resolved": "/ops/reviews/{review_id}/resolve",
                "archive": "/ops/reviews/{review_id}/archive",
            },
        }

    def decide(self, review_id: str, decision: str, reason: str = "") -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        selected: dict[str, Any] | None = None
        changed = False

        def apply_decision(state: dict[str, Any]) -> None:
            nonlocal selected, changed
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            for item in items:
                if not isinstance(item, dict) or item.get("review_id") != review_id:
                    continue
                if item.get("status") == "pending":
                    item["status"] = decision
                    item["decided_at"] = utc_now_iso()
                    item["decision_reason"] = reason
                    changed = True
                selected = dict(item)
                return
            raise KeyError(f"Review not found: {review_id}")

        self.state_store.mutate_json("review_queue.json", apply_decision)
        if selected is None:
            raise KeyError(f"Review not found: {review_id}")
        if changed:
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "event_id": selected.get("event_id"),
                    "route": "human_review",
                    "status": decision,
                    "artifacts": {"review_id": review_id, "reason": reason},
                },
            )
            self.state_store.patch_json(
                "risk_state.json",
                {"current_risk": selected.get("risk_level", "R0") if decision == "approved" else "R0"},
            )
        return selected

    def approve_and_claim(
        self,
        review_id: str,
        reason: str = "",
    ) -> tuple[dict[str, Any], str | None]:
        """Approve a review and reserve its side effect for exactly one caller.

        The raw claim token is returned once and is never persisted. A claim left
        without an execution observation remains indeterminate and cannot be
        claimed again automatically.
        """

        claim_token = secrets.token_urlsafe(32)
        token_digest = _claim_token_digest(claim_token)
        selected: dict[str, Any] | None = None
        approved = False
        claimed = False

        def approve_and_reserve(state: dict[str, Any]) -> None:
            nonlocal selected, approved, claimed
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            for item in items:
                if not isinstance(item, dict) or item.get("review_id") != review_id:
                    continue
                status = str(item.get("status") or "")
                if status == "pending":
                    item["status"] = "approved"
                    item["decided_at"] = utc_now_iso()
                    item["decision_reason"] = reason
                    approved = True
                    status = "approved"
                if (
                    status == "approved"
                    and item.get("execution_result") is None
                    and not isinstance(item.get("execution_claim"), dict)
                ):
                    item["execution_claim"] = {
                        "token_digest": token_digest,
                        "proposal_digest": _canonical_digest(item.get("proposal")),
                        "claimed_at": utc_now_iso(),
                        "state": "indeterminate",
                    }
                    claimed = True
                selected = dict(item)
                return
            raise KeyError(f"Review not found: {review_id}")

        self.state_store.mutate_json("review_queue.json", approve_and_reserve)
        if selected is None:
            raise KeyError(f"Review not found: {review_id}")
        if approved:
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "event_id": selected.get("event_id"),
                    "route": "human_review",
                    "status": "approved",
                    "artifacts": {"review_id": review_id, "reason": reason},
                },
            )
            self.state_store.patch_json(
                "risk_state.json",
                {"current_risk": selected.get("risk_level", "R0")},
            )
        if claimed:
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "event_id": selected.get("event_id"),
                    "route": "human_review_execution",
                    "status": "execution_claimed",
                    "artifacts": {
                        "review_id": review_id,
                        "proposal_digest": selected["execution_claim"][
                            "proposal_digest"
                        ],
                    },
                },
            )
        return selected, claim_token if claimed else None

    def authorize_execution(
        self,
        review_id: str,
        claim_token: str,
    ) -> dict[str, Any]:
        """Consume a review claim at the executor boundary.

        The review supplied by an HTTP caller or another runtime component is
        never an authority object.  This method reloads the canonical queue
        entry, validates the one-time bearer claim and proposal digest, then
        atomically moves the claim to ``executing``.  A crash after this point
        remains indeterminate and cannot be replayed automatically.
        """

        normalized_review_id = str(review_id or "")
        normalized_token = str(claim_token or "")
        if (
            not normalized_review_id
            or normalized_review_id != normalized_review_id.strip()
        ):
            raise PermissionError("review_id is required for execution")
        if (
            not normalized_token
            or normalized_token != normalized_token.strip()
            or len(normalized_token) > 512
        ):
            raise PermissionError(
                f"Execution claim token required for review: {normalized_review_id}"
            )

        selected: dict[str, Any] | None = None

        def authorize(state: dict[str, Any]) -> None:
            nonlocal selected
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            for item in items:
                if (
                    not isinstance(item, dict)
                    or item.get("review_id") != normalized_review_id
                ):
                    continue
                if item.get("status") != "approved":
                    raise PermissionError(
                        "Only an approved canonical review can authorize "
                        f"execution: {normalized_review_id}"
                    )
                if isinstance(item.get("execution_result"), dict):
                    raise PermissionError(
                        f"Review execution is already observed: {normalized_review_id}"
                    )
                claim = item.get("execution_claim")
                if not isinstance(claim, dict):
                    raise PermissionError(
                        f"Execution claim is missing for review: {normalized_review_id}"
                    )
                expected_token_digest = str(claim.get("token_digest") or "")
                if not expected_token_digest or not secrets.compare_digest(
                    expected_token_digest,
                    _claim_token_digest(normalized_token),
                ):
                    raise PermissionError(
                        "Execution claim token does not match review: "
                        f"{normalized_review_id}"
                    )
                proposal_digest = _canonical_digest(item.get("proposal"))
                if not secrets.compare_digest(
                    str(claim.get("proposal_digest") or ""),
                    proposal_digest,
                ):
                    raise ValueError(
                        "Review proposal changed after execution claim: "
                        f"{normalized_review_id}"
                    )
                if claim.get("state") != "indeterminate":
                    raise PermissionError(
                        "Execution claim is not available for one-time use: "
                        f"{normalized_review_id}"
                    )
                claim["state"] = "executing"
                claim["authorized_at"] = utc_now_iso()
                selected = copy.deepcopy(item)
                return
            raise KeyError(f"Review not found: {normalized_review_id}")

        self.state_store.mutate_json("review_queue.json", authorize)
        if selected is None:
            raise KeyError(f"Review not found: {normalized_review_id}")
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "event_id": selected.get("event_id"),
                "route": "human_review_execution",
                "status": "execution_authorized",
                "artifacts": {
                    "review_id": normalized_review_id,
                    "proposal_digest": (
                        selected.get("execution_claim") or {}
                    ).get("proposal_digest"),
                },
            },
        )
        return selected

    def mark_resolved(self, review_id: str, reason: str = "") -> dict[str, Any]:
        return self._set_terminal_status(review_id, "resolved", reason or "marked resolved by runtime hygiene")

    def archive(self, review_id: str, reason: str = "") -> dict[str, Any]:
        review = self._set_terminal_status(review_id, "archived", reason or "archived by runtime hygiene")
        archive_path = self._archive_review(review)
        review["archive_path"] = archive_path

        def attach_archive_path(state: dict[str, Any]) -> None:
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            for item in items:
                if isinstance(item, dict) and item.get("review_id") == review_id:
                    item["archive_path"] = archive_path
                    return
            raise KeyError(f"Review not found: {review_id}")

        self.state_store.mutate_json("review_queue.json", attach_archive_path)
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "event_id": review.get("event_id"),
                "route": "human_review_archive",
                "status": "archived",
                "artifacts": {"review_id": review_id, "reason": reason, "archive_path": archive_path},
            },
        )
        return review

    def update_execution(
        self,
        review_id: str,
        execution_result: dict[str, Any],
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(execution_result, dict) or not execution_result:
            raise ValueError("execution_result must be a non-empty object")
        incoming_result_digest = _canonical_digest(execution_result)
        audit_status = _execution_audit_status(execution_result)
        selected: dict[str, Any] | None = None
        changed = False

        def apply_execution(state: dict[str, Any]) -> None:
            nonlocal selected, changed
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            for item in items:
                if not isinstance(item, dict) or item.get("review_id") != review_id:
                    continue
                if item.get("status") != "approved":
                    raise PermissionError(
                        f"Only an approved review can record execution: {review_id}"
                    )
                claim = item.get("execution_claim")
                if not isinstance(claim, dict) or not claim_token:
                    raise PermissionError(
                        f"Execution claim token required for review: {review_id}"
                    )
                expected_digest = str(claim.get("token_digest") or "")
                if not secrets.compare_digest(
                    expected_digest,
                    _claim_token_digest(claim_token),
                ):
                    raise PermissionError(
                        f"Execution claim token does not match review: {review_id}"
                    )
                proposal_digest = _canonical_digest(item.get("proposal"))
                if not secrets.compare_digest(
                    str(claim.get("proposal_digest") or ""),
                    proposal_digest,
                ):
                    raise ValueError(
                        f"Review proposal changed after execution claim: {review_id}"
                    )
                existing_result = item.get("execution_result")
                if isinstance(existing_result, dict):
                    if claim.get("state") != "observed":
                        raise ValueError(
                            "Review has an execution result without an observed "
                            f"claim: {review_id}"
                        )
                    if _canonical_digest(existing_result) != (
                        incoming_result_digest
                    ):
                        raise ValueError(
                            f"Execution result already recorded for review: {review_id}"
                        )
                    selected = dict(item)
                    return
                if claim.get("state") != "executing":
                    raise PermissionError(
                        "Execution result can only follow executor-boundary "
                        f"authorization: {review_id}"
                    )
                item["execution_result"] = execution_result
                claim["state"] = "observed"
                claim["observed_at"] = utc_now_iso()
                changed = True
                selected = dict(item)
                return
            raise KeyError(f"Review not found: {review_id}")

        self.state_store.mutate_json("review_queue.json", apply_execution)
        if selected is None:
            raise KeyError(f"Review not found: {review_id}")
        if not changed:
            return selected
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "event_id": selected.get("event_id"),
                "route": "human_review",
                "status": audit_status,
                "artifacts": {"review_id": review_id, "execution_result": execution_result},
            },
        )
        return selected

    def _set_terminal_status(self, review_id: str, status: str, reason: str) -> dict[str, Any]:
        if status not in {"resolved", "archived"}:
            raise ValueError("status must be resolved or archived")
        selected: dict[str, Any] | None = None
        changed = False

        def apply_terminal_status(state: dict[str, Any]) -> None:
            nonlocal selected, changed
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            for item in items:
                if not isinstance(item, dict) or item.get("review_id") != review_id:
                    continue
                if item.get("status") == "pending":
                    item["status"] = status
                    item["decided_at"] = utc_now_iso()
                    item["decision_reason"] = reason
                    changed = True
                selected = dict(item)
                return
            raise KeyError(f"Review not found: {review_id}")

        self.state_store.mutate_json("review_queue.json", apply_terminal_status)
        if selected is None:
            raise KeyError(f"Review not found: {review_id}")
        if changed:
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "event_id": selected.get("event_id"),
                    "route": "human_review",
                    "status": status,
                    "artifacts": {"review_id": review_id, "reason": reason},
                },
            )
            self.state_store.patch_json("risk_state.json", {"current_risk": "R0"})
        return selected

    def _diagnostic_item(self, item: dict[str, Any], *, stale_after_days: int) -> dict[str, Any]:
        created_at = str(item.get("created_at") or "")
        age_seconds = self._age_seconds(created_at)
        review_type = self._review_type(item)
        source = self._source(item)
        risk = str(item.get("risk_level") or "unknown")
        stale = age_seconds is not None and age_seconds > max(1, stale_after_days) * 86400
        return {
            "review_id": item.get("review_id"),
            "type": review_type,
            "source": source,
            "created_at": created_at,
            "age_seconds": age_seconds,
            "stale": stale,
            "risk": risk,
            "risk_level": risk,
            "status": item.get("status"),
            "task_text": str(item.get("task_text") or "")[:240],
            "guardian_decision": (item.get("guardian_decision") or {}).get("decision") if isinstance(item.get("guardian_decision"), dict) else None,
            "can_mark_resolved": stale,
            "can_archive": stale,
        }

    def _review_type(self, item: dict[str, Any]) -> str:
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        if proposal.get("type"):
            return str(proposal.get("type"))
        text = str(item.get("task_text") or "").lower()
        if "openclaw" in text and ("重启" in text or "restart" in text):
            return "service_restart_request"
        if "删除" in text or "rm -rf" in text or "delete" in text:
            return "destructive_action_review"
        return "action_review"

    def _source(self, item: dict[str, Any]) -> str:
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        for key in ("source", "route", "origin"):
            if proposal.get(key):
                return str(proposal.get(key))
        event_id = str(item.get("event_id") or "")
        return f"event:{event_id}" if event_id else "guardian_review_queue"

    def _age_seconds(self, value: str) -> int | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))

    def _archive_review(self, review: dict[str, Any]) -> str:
        review_id = str(review.get("review_id") or f"review_{uuid4().hex[:12]}")
        archive_root = self.state_store.root / "archive" / "reviews"
        archive_root.mkdir(parents=True, exist_ok=True)
        timestamp = utc_now_iso().replace(":", "").replace(".", "")
        archive_path = archive_root / f"{review_id}-{timestamp}.json"
        archive_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            return str(archive_path.relative_to(self.state_store.root))
        except ValueError:
            return str(Path(archive_path))
