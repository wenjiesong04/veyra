"""Pure state schema and codecs for the trusted workspace observer.

This module intentionally has no store, filesystem, HTTP, or observer-service
dependencies.  It validates the small durable document used by the producer
and provides deterministic codecs shared by the service and delivery layer.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any

from interface.structured_observation import canonical_digest
from runtime.isolated_git_snapshot import GitWorkspaceObservation


STATE_FILE = "trusted_workspace_observer_state.json"
SCHEMA_VERSION = "veyra.trusted_workspace_observer.v1"
CONFIG_SCHEMA_VERSION = "veyra.trusted_workspace_observer.binding.v1"
MAX_VALIDITY_SECONDS = 60 * 60

OBSERVER_STATE_KEYS = frozenset(
    {
        "schema_version",
        "binding",
        "baseline",
        "last_observation",
        "active_change",
        "pending_delivery",
        "last_run",
        "updated_at",
        "source",
        "confidence",
        "ttl_seconds",
        "status",
        "_state_revision",
    }
)
BINDING_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "user_id",
        "session_id",
        "workspace_digest",
        "repo_id",
        "origin_digest",
        "origin_host",
        "full_ref",
        "goal_id",
        "goal_digest",
        "goal_revision",
        "goal_priority",
        "ci_binding",
        "binding_generation",
    }
)
OBSERVATION_KEYS = frozenset(
    {
        "revision",
        "full_ref",
        "repo_id",
        "origin_digest",
        "origin_host",
        "dirty",
        "category_counts",
        "manifest_digest",
        "subject",
        "ci_signal_state",
        "ci_target_sha",
        "ci_target_ref",
        "ci_probe_digest",
        "observed_at",
    }
)
ACTIVE_KEYS = frozenset(
    {
        "subject",
        "changed_at",
        "risk_emitted",
    }
)
RUN_KEYS = frozenset(
    {
        "status",
        "reason",
        "trigger",
        "events_published",
        "events_attempted",
        "category_counts",
        "dirty",
        "binding_generation",
    }
)
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9_.\-/]{1,220}$")
REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SUBJECT_RE = re.compile(r"^[0-9a-f]{48}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
RECEIPT_SUFFIX_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
CATEGORY_KEYS = frozenset({"code", "test", "docs", "config", "other"})


class StateValidationError(ValueError):
    """Raised when a durable observer document is not the supported schema."""


def digest(value: Any) -> str:
    return canonical_digest(value)


def state_revision(state: dict[str, Any]) -> int:
    raw = state.get("_state_revision") if isinstance(state, dict) else 0
    return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0


def observation_record(
    snapshot: GitWorkspaceObservation,
    subject: str,
    ci_fact: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "revision": snapshot.revision,
        "full_ref": snapshot.full_ref,
        "repo_id": snapshot.repo_id,
        "origin_digest": snapshot.origin_digest,
        "origin_host": str(snapshot.origin_host or ""),
        "dirty": bool(snapshot.dirty),
        "category_counts": dict(snapshot.category_counts),
        "manifest_digest": snapshot.manifest_digest,
        "subject": subject,
        "ci_signal_state": ci_fact.get("signal_state") if ci_fact else None,
        "ci_target_sha": ci_fact.get("target_sha") if ci_fact else None,
        "ci_target_ref": ci_fact.get("target_ref") if ci_fact else None,
        "ci_probe_digest": ci_fact.get("probe_digest") if ci_fact else None,
        "observed_at": snapshot.captured_at,
    }


def validate_state(state: dict[str, Any]) -> None:
    if not isinstance(state, dict) or state.get("_state_corrupt") is True:
        raise StateValidationError("observer state is corrupt")
    if str(state.get("schema_version") or "") != SCHEMA_VERSION:
        raise StateValidationError("observer state schema is invalid")
    if any(key not in OBSERVER_STATE_KEYS for key in state):
        raise StateValidationError("observer state contains unknown fields")
    revision = state.get("_state_revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise StateValidationError("observer state revision is invalid")
    binding = state.get("binding")
    if binding is not None:
        validate_binding_record(binding)
    for key in ("baseline", "last_observation"):
        record = state.get(key)
        if record is not None:
            validate_observation_record(record)
    active = state.get("active_change")
    if active is not None:
        if not isinstance(active, dict) or any(key not in ACTIVE_KEYS for key in active):
            raise StateValidationError("observer active state is invalid")
        subject = str(active.get("subject") or "")
        if not SUBJECT_RE.fullmatch(subject):
            raise StateValidationError("observer active subject is invalid")
        _parse_timestamp(active.get("changed_at"), "observer active changed_at")
        if not isinstance(active.get("risk_emitted"), bool):
            raise StateValidationError("observer active flag is invalid")
    pending = state.get("pending_delivery")
    if pending is not None:
        validate_pending_delivery(pending)
    last_run = state.get("last_run")
    if last_run is not None:
        if not isinstance(last_run, dict) or any(key not in RUN_KEYS for key in last_run):
            raise StateValidationError("observer run state is invalid")
        published = last_run.get("events_published", 0)
        attempted = last_run.get("events_attempted", 0)
        if (
            isinstance(published, bool)
            or not isinstance(published, int)
            or isinstance(attempted, bool)
            or not isinstance(attempted, int)
            or published < 0
            or attempted < 0
            or published > attempted
        ):
            raise StateValidationError("observer run counters are invalid")


def validate_binding_record(binding: dict[str, Any]) -> None:
    if not isinstance(binding, dict) or any(key not in BINDING_KEYS for key in binding):
        raise StateValidationError("observer binding contains unknown fields")
    if str(binding.get("schema_version") or "") != CONFIG_SCHEMA_VERSION:
        raise StateValidationError("observer binding schema is invalid")
    if str(binding.get("status") or "") not in {"disabled", "record_only"}:
        raise StateValidationError("observer binding mode is invalid")
    generation = binding.get("binding_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise StateValidationError("observer binding generation is invalid")
    for key in (
        "user_id",
        "session_id",
        "workspace_digest",
        "repo_id",
        "origin_digest",
        "origin_host",
        "full_ref",
        "goal_id",
        "goal_digest",
        "goal_revision",
    ):
        if not isinstance(binding.get(key), str) or (
            not binding.get(key) and key not in {"origin_host", "goal_revision"}
        ):
            raise StateValidationError(f"observer binding {key} is invalid")
    if (
        not DIGEST_RE.fullmatch(str(binding["workspace_digest"]))
        or not DIGEST_RE.fullmatch(str(binding["origin_digest"]))
        or not DIGEST_RE.fullmatch(str(binding["goal_digest"]))
    ):
        raise StateValidationError("observer binding digest is invalid")
    if not REF_RE.fullmatch(str(binding["full_ref"])):
        raise StateValidationError("observer binding ref is invalid")
    if str(binding["origin_host"]) not in {"", "github.com"}:
        raise StateValidationError("observer binding origin host is invalid")
    priority = binding.get("goal_priority")
    if isinstance(priority, bool) or not isinstance(priority, (int, float)) or not 0.0 <= float(priority) <= 1.0:
        raise StateValidationError("observer binding goal priority is invalid")
    ci = binding.get("ci_binding")
    if ci is not None and not isinstance(ci, dict):
        raise StateValidationError("observer CI binding is invalid")
    if ci is not None:
        if str(binding["origin_host"]) != "github.com":
            raise StateValidationError("observer CI binding requires GitHub origin")
        if str(ci.get("api_origin") or "") != "https://api.github.com":
            raise StateValidationError("observer CI provider origin is invalid")
        if str(ci.get("repo_id") or "").lower() != str(binding["repo_id"]).lower():
            raise StateValidationError("observer CI repository is invalid")


def validate_observation_record(record: dict[str, Any]) -> None:
    if not isinstance(record, dict) or any(key not in OBSERVATION_KEYS for key in record):
        raise StateValidationError("observer observation contains unknown fields")
    for key in (
        "revision",
        "full_ref",
        "repo_id",
        "origin_digest",
        "origin_host",
        "manifest_digest",
        "subject",
        "observed_at",
    ):
        if not isinstance(record.get(key), str) or (
            not record.get(key) and key not in {"origin_host"}
        ):
            raise StateValidationError("observer observation identity is invalid")
    if not REVISION_RE.fullmatch(str(record["revision"])):
        raise StateValidationError("observer observation revision is invalid")
    if not REPO_RE.fullmatch(str(record["repo_id"])):
        raise StateValidationError("observer observation repository is invalid")
    if str(record["origin_host"]) not in {"", "github.com"}:
        raise StateValidationError("observer observation origin is invalid")
    if not SUBJECT_RE.fullmatch(str(record["subject"])):
        raise StateValidationError("observer observation subject is invalid")
    _parse_timestamp(record["observed_at"], "observer observation observed_at")
    if not DIGEST_RE.fullmatch(str(record["origin_digest"])) or not DIGEST_RE.fullmatch(str(record["manifest_digest"])):
        raise StateValidationError("observer observation digest is invalid")
    if not REF_RE.fullmatch(str(record["full_ref"])):
        raise StateValidationError("observer observation ref is invalid")
    if not isinstance(record.get("dirty"), bool):
        raise StateValidationError("observer observation dirty flag is invalid")
    counts = record.get("category_counts")
    if not isinstance(counts, dict) or set(counts) != CATEGORY_KEYS or any(
        key not in CATEGORY_KEYS
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for key, value in counts.items()
    ):
        raise StateValidationError("observer observation category counts are invalid")
    ci_state = record.get("ci_signal_state")
    if ci_state not in {None, "clear", "present"}:
        raise StateValidationError("observer CI signal state is invalid")
    ci_digest = record.get("ci_probe_digest")
    if ci_digest is not None and not DIGEST_RE.fullmatch(str(ci_digest)):
        raise StateValidationError("observer CI probe digest is invalid")
    if ci_state is not None:
        if str(record.get("ci_target_sha") or "") != str(record.get("revision") or ""):
            raise StateValidationError("observer CI target SHA is invalid")
        if str(record.get("ci_target_ref") or "") != str(record.get("full_ref") or ""):
            raise StateValidationError("observer CI target ref is invalid")
    elif any(
        record.get(key) is not None
        for key in ("ci_target_sha", "ci_target_ref", "ci_probe_digest")
    ):
        raise StateValidationError("observer observation CI binding is partial")


def validate_pending_delivery(pending: dict[str, Any]) -> None:
    allowed = {
        "subject",
        "workspace_digest",
        "binding_generation",
        "occurred_at",
        "valid_until",
        "required_kinds",
        "acknowledged_kinds",
        "receipt_suffix",
        "snapshot_record",
        "ci_fact",
        "delivery_epoch",
    }
    if not isinstance(pending, dict) or any(key not in allowed for key in pending):
        raise StateValidationError("observer pending delivery is invalid")
    if not isinstance(pending.get("subject"), str) or not pending["subject"]:
        raise StateValidationError("observer pending subject is invalid")
    workspace_digest = pending.get("workspace_digest")
    if not isinstance(workspace_digest, str) or not DIGEST_RE.fullmatch(workspace_digest):
        raise StateValidationError("observer pending workspace is invalid")
    generation = pending.get("binding_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise StateValidationError("observer pending generation is invalid")
    required = pending.get("required_kinds")
    acknowledged = pending.get("acknowledged_kinds")
    if (
        not isinstance(required, list)
        or not required
        or len(required) > 2
        or any(item not in {"change_signal", "risk_signal"} for item in required)
        or len(set(required)) != len(required)
        or not isinstance(acknowledged, list)
        or any(item not in required for item in acknowledged)
        or len(set(acknowledged)) != len(acknowledged)
    ):
        raise StateValidationError("observer pending kinds are invalid")
    if not isinstance(pending.get("receipt_suffix"), str) or not RECEIPT_SUFFIX_RE.fullmatch(pending["receipt_suffix"]):
        raise StateValidationError("observer pending receipt suffix is invalid")
    occurred = _parse_timestamp(pending.get("occurred_at"), "observer pending occurred_at")
    valid_until = _parse_timestamp(pending.get("valid_until"), "observer pending valid_until")
    if valid_until < occurred:
        raise StateValidationError("observer pending validity window is invalid")
    if (valid_until - occurred).total_seconds() > MAX_VALIDITY_SECONDS:
        raise StateValidationError("observer pending validity window is too large")
    snapshot_record = pending.get("snapshot_record")
    if not isinstance(snapshot_record, dict):
        raise StateValidationError("observer pending snapshot is invalid")
    validate_observation_record(snapshot_record)
    if str(snapshot_record.get("subject") or "") != str(pending.get("subject") or ""):
        raise StateValidationError("observer pending subject does not match snapshot")
    ci_fact = pending.get("ci_fact")
    if ci_fact is not None:
        _validate_ci_fact(ci_fact, snapshot_record)
    epoch = pending.get("delivery_epoch", 0)
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise StateValidationError("observer pending delivery epoch is invalid")


def snapshot_from_record(record: dict[str, Any]) -> GitWorkspaceObservation:
    validate_observation_record(record)
    return GitWorkspaceObservation(
        repository_root="",
        repo_id=str(record.get("repo_id") or ""),
        origin_digest=str(record.get("origin_digest") or ""),
        full_ref=str(record.get("full_ref") or ""),
        revision=str(record.get("revision") or ""),
        dirty=bool(record.get("dirty")),
        category_counts=dict(record.get("category_counts") or {}),
        manifest_digest=str(record.get("manifest_digest") or ""),
        captured_at=str(record.get("observed_at") or ""),
        object_format="sha256" if len(str(record.get("revision") or "")) == 64 else "sha1",
        origin_host=str(record.get("origin_host") or ""),
    )


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise StateValidationError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise StateValidationError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StateValidationError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _validate_ci_fact(ci_fact: dict[str, Any], snapshot_record: dict[str, Any]) -> None:
    required = {"signal_state", "repo_id", "target_ref", "target_sha", "probe_digest"}
    if not isinstance(ci_fact, dict) or not required.issubset(ci_fact):
        raise StateValidationError("observer pending CI fact is invalid")
    if any(key not in required | {"run_id", "run_number", "run_attempt", "check_suite_id", "policy_digest"} for key in ci_fact):
        raise StateValidationError("observer pending CI fact contains unknown fields")
    if ci_fact.get("signal_state") not in {"clear", "present"}:
        raise StateValidationError("observer pending CI signal state is invalid")
    if str(ci_fact.get("target_sha") or "") != str(snapshot_record.get("revision") or ""):
        raise StateValidationError("observer pending CI target SHA is invalid")
    if str(ci_fact.get("target_ref") or "") != str(snapshot_record.get("full_ref") or ""):
        raise StateValidationError("observer pending CI target ref is invalid")
    if str(ci_fact.get("repo_id") or "").lower() != str(snapshot_record.get("repo_id") or "").lower():
        raise StateValidationError("observer pending CI repository is invalid")
    if not DIGEST_RE.fullmatch(str(ci_fact.get("probe_digest") or "")):
        raise StateValidationError("observer pending CI probe digest is invalid")


def copy_pending(pending: dict[str, Any]) -> dict[str, Any]:
    validate_pending_delivery(pending)
    return copy.deepcopy(pending)


__all__ = [
    "ACTIVE_KEYS",
    "BINDING_KEYS",
    "CONFIG_SCHEMA_VERSION",
    "DIGEST_RE",
    "MAX_VALIDITY_SECONDS",
    "OBSERVATION_KEYS",
    "OBSERVER_STATE_KEYS",
    "REF_RE",
    "RUN_KEYS",
    "SCHEMA_VERSION",
    "STATE_FILE",
    "StateValidationError",
    "copy_pending",
    "digest",
    "observation_record",
    "snapshot_from_record",
    "state_revision",
    "validate_binding_record",
    "validate_observation_record",
    "validate_pending_delivery",
    "validate_state",
]
