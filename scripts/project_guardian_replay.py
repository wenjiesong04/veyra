#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.project_guardian import ProjectGuardianEvaluator


MANIFEST_SCHEMA = "veyra.project_guardian_replay_manifest.v1"
EPISODES_SCHEMA = "veyra.project_guardian_replay_episodes.v1"
LABELS_SCHEMA = "veyra.project_guardian_replay_labels.v1"
REVIEWS_SCHEMA = "veyra.project_guardian_replay_reviews.v1"
PREDICTIONS_SCHEMA = "veyra.project_guardian_replay_predictions.v1"
REPORT_SCHEMA = "veyra.project_guardian_replay_report.v1"

MIN_POSITIVE_GROUPS = 20
MIN_NEGATIVE_GROUPS = 20
MIN_HUMAN_REVIEWED_GROUPS = 20
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_EPISODES = 10_000

THRESHOLDS = {
    "precision": 0.95,
    "recall": 0.85,
    "false_association_count": 0,
    "evidence_correctness": 1.0,
    "human_usefulness": 0.8,
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_EXACT_FIELDS = {
    "body",
    "command",
    "content",
    "cwd",
    "details",
    "diff",
    "file_path",
    "filepath",
    "filename",
    "log",
    "logs",
    "message",
    "patch",
    "path",
    "paths",
    "prompt",
    "raw",
    "raw_diff",
    "raw_log",
    "raw_text",
    "stderr",
    "stdout",
    "text",
    "user_text",
    "worktree",
}
_FORBIDDEN_FIELD_SUFFIXES = (
    "_body",
    "_content",
    "_diff",
    "_log",
    "_logs",
    "_message",
    "_patch",
    "_path",
    "_paths",
    "_prompt",
    "_text",
)
_LABEL_LEAK_FIELDS = {
    "adjudication",
    "expected",
    "expected_candidates",
    "ground_truth",
    "human_reviews",
    "label",
    "labels",
    "outcome",
    "rating",
    "ratings",
    "truth",
    "useful",
    "usefulness",
}


class ProtocolError(ValueError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"artifact is not canonical JSON: {exc}") from exc


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def file_sha256(path: Path | str) -> str:
    selected = Path(path)
    return hashlib.sha256(
        _read_frozen_bytes(selected, artifact=selected.name or "artifact")
    ).hexdigest()


def _read_frozen_bytes(path: Path | str, *, artifact: str) -> bytes:
    selected = Path(path)
    try:
        with selected.open("rb") as handle:
            descriptor = os.fstat(handle.fileno())
            if not stat.S_ISREG(descriptor.st_mode):
                raise ProtocolError(f"{artifact} must be a regular file")
            if descriptor.st_size > MAX_ARTIFACT_BYTES:
                raise ProtocolError(
                    f"{artifact} exceeds {MAX_ARTIFACT_BYTES} bytes"
                )
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise ProtocolError(f"invalid {artifact}: {exc}") from exc
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ProtocolError(
            f"{artifact} exceeds {MAX_ARTIFACT_BYTES} bytes"
        )
    return raw


def _load_frozen_json(
    path: Path | str,
    *,
    artifact: str,
) -> tuple[Any, str]:
    raw = _read_frozen_bytes(path, artifact=artifact)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid {artifact}: {exc}") from exc
    _validate_finite_json(value, location=artifact)
    return value, hashlib.sha256(raw).hexdigest()


def _unique_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for key, value in pairs:
        if key in selected:
            raise ProtocolError("duplicate JSON object key")
        selected[key] = value
    return selected


def _load_json(path: Path | str, *, artifact: str) -> Any:
    value, _ = _load_frozen_json(path, artifact=artifact)
    return value


def _validate_finite_json(value: Any, *, location: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ProtocolError(f"non-finite number at {location}")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError(f"non-string object key at {location}")
            _validate_finite_json(item, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_finite_json(item, location=f"{location}[{index}]")


def _write_frozen_json(path: Path | str, value: dict[str, Any]) -> None:
    selected = Path(path)
    if not selected.parent.is_dir():
        raise ProtocolError("output parent directory does not exist")
    try:
        with selected.open("x", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
    except FileExistsError as exc:
        raise ProtocolError(
            f"refusing to overwrite frozen artifact: {selected.name}"
        ) from exc


def _exact_keys(
    value: Any,
    *,
    required: set[str],
    optional: set[str] | None = None,
    location: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{location} must be an object")
    optional = optional or set()
    keys = set(value)
    missing = required - keys
    unexpected = keys - required - optional
    if missing:
        raise ProtocolError(
            f"{location} missing fields: {sorted(missing)}"
        )
    if unexpected:
        raise ProtocolError(
            f"{location} has unexpected fields: {sorted(unexpected)}"
        )
    return value


def _text(value: Any, *, location: str, limit: int = 300) -> str:
    selected = value.strip() if isinstance(value, str) else ""
    if not selected or len(selected) > limit:
        raise ProtocolError(
            f"{location} must be a non-empty string of at most {limit} characters"
        )
    return selected


def _positive_int(value: Any, *, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProtocolError(f"{location} must be a positive integer")
    return value


def _sha256_text(value: Any, *, location: str) -> str:
    selected = _text(value, location=location, limit=64).lower()
    if not _SHA256_RE.fullmatch(selected):
        raise ProtocolError(f"{location} must be a lowercase SHA-256 digest")
    return selected


def _aware_time(value: Any, *, location: str) -> str:
    selected = _text(value, location=location, limit=80)
    try:
        parsed = datetime.fromisoformat(selected.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError(f"{location} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolError(f"{location} must include a timezone")
    normalized = parsed.astimezone(timezone.utc).isoformat()
    if selected != normalized:
        raise ProtocolError(
            f"{location} must use canonical UTC ISO-8601 form"
        )
    return normalized


def _normalized_field(value: str) -> str:
    return value.strip().casefold().replace("-", "_")


def _reject_leakage_fields(
    value: Any,
    *,
    location: str,
    reject_labels: bool,
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = _normalized_field(key)
            forbidden = (
                normalized in _FORBIDDEN_EXACT_FIELDS
                or normalized.startswith("raw_")
                or normalized.endswith(_FORBIDDEN_FIELD_SUFFIXES)
            )
            if forbidden:
                raise ProtocolError(
                    f"raw text/diff/log/path leakage field rejected at "
                    f"{location}.{key}"
                )
            if reject_labels and normalized in _LABEL_LEAK_FIELDS:
                raise ProtocolError(
                    f"label leakage field rejected at {location}.{key}"
                )
            _reject_leakage_fields(
                item,
                location=f"{location}.{key}",
                reject_labels=reject_labels,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_leakage_fields(
                item,
                location=f"{location}[{index}]",
                reject_labels=reject_labels,
            )


def _manifest(path: Path | str) -> tuple[dict[str, Any], str]:
    selected = Path(path)
    loaded, actual_sha256 = _load_frozen_json(
        selected,
        artifact="manifest",
    )
    value = _exact_keys(
        loaded,
        required={
            "schema_version",
            "dataset_id",
            "split",
            "data_class",
            "anonymization_version",
            "label_policy_version",
            "episodes_sha256",
            "labels_sha256",
        },
        location="manifest",
    )
    if value["schema_version"] != MANIFEST_SCHEMA:
        raise ProtocolError("unsupported manifest schema")
    _text(value["dataset_id"], location="manifest.dataset_id", limit=200)
    _text(
        value["anonymization_version"],
        location="manifest.anonymization_version",
        limit=120,
    )
    _text(
        value["label_policy_version"],
        location="manifest.label_policy_version",
        limit=120,
    )
    if value["split"] != "held_out":
        raise ProtocolError("manifest.split must be held_out")
    if value["data_class"] not in {"real_project", "synthetic", "mixed"}:
        raise ProtocolError(
            "manifest.data_class must be real_project, synthetic, or mixed"
        )
    _sha256_text(
        value["episodes_sha256"],
        location="manifest.episodes_sha256",
    )
    _sha256_text(
        value["labels_sha256"],
        location="manifest.labels_sha256",
    )
    return value, actual_sha256


def _episodes(
    manifest: dict[str, Any],
    path: Path | str,
) -> tuple[list[dict[str, Any]], str]:
    selected = Path(path)
    loaded, actual_sha256 = _load_frozen_json(
        selected,
        artifact="episodes",
    )
    if actual_sha256 != manifest["episodes_sha256"]:
        raise ProtocolError("episodes SHA-256 does not match manifest")
    value = _exact_keys(
        loaded,
        required={"schema_version", "dataset_id", "episodes"},
        location="episodes",
    )
    if value["schema_version"] != EPISODES_SCHEMA:
        raise ProtocolError("unsupported episodes schema")
    if value["dataset_id"] != manifest["dataset_id"]:
        raise ProtocolError("episodes dataset_id does not match manifest")
    _reject_leakage_fields(
        value,
        location="episodes",
        reject_labels=True,
    )
    raw_episodes = value["episodes"]
    if not isinstance(raw_episodes, list):
        raise ProtocolError("episodes.episodes must be a list")
    if len(raw_episodes) > MAX_EPISODES:
        raise ProtocolError(f"episode count exceeds {MAX_EPISODES}")

    accepted: list[dict[str, Any]] = []
    ids: set[str] = set()
    group_ids: set[str] = set()
    source_fingerprints: set[str] = set()
    semantic_input_digests: set[str] = set()
    classes: set[str] = set()
    for index, raw in enumerate(raw_episodes):
        episode = _exact_keys(
            raw,
            required={
                "episode_id",
                "group_id",
                "source_fingerprint",
                "source_class",
                "evaluated_at",
                "goals_state",
                "event_inbox_state",
            },
            location=f"episodes.episodes[{index}]",
        )
        episode_id = _text(
            episode["episode_id"],
            location=f"episodes.episodes[{index}].episode_id",
            limit=200,
        )
        if episode_id in ids:
            raise ProtocolError(f"duplicate episode_id: {episode_id}")
        ids.add(episode_id)
        group_id = _text(
            episode["group_id"],
            location=f"episodes.episodes[{index}].group_id",
            limit=200,
        )
        if group_id in group_ids:
            raise ProtocolError(f"duplicate group_id: {group_id}")
        group_ids.add(group_id)
        source_fingerprint = _sha256_text(
            episode["source_fingerprint"],
            location=f"episodes.episodes[{index}].source_fingerprint",
        )
        if source_fingerprint in source_fingerprints:
            raise ProtocolError(
                f"duplicate source_fingerprint: {source_fingerprint}"
            )
        source_fingerprints.add(source_fingerprint)
        source_class = episode["source_class"]
        if source_class not in {"real_project", "synthetic"}:
            raise ProtocolError(
                f"invalid source_class for episode {episode_id}"
            )
        classes.add(source_class)
        evaluated_at = _aware_time(
            episode["evaluated_at"],
            location=f"episodes.episodes[{index}].evaluated_at",
        )
        goals_state = _canonical_goals_state(
            episode["goals_state"],
            location=f"episodes.episodes[{index}].goals_state",
        )
        event_inbox_state = _canonical_event_inbox_state(
            episode["event_inbox_state"],
            location=f"episodes.episodes[{index}].event_inbox_state",
        )
        semantic_input_digest = _semantic_evaluator_input_digest(
            evaluated_at=evaluated_at,
            goals_state=goals_state,
            event_inbox_state=event_inbox_state,
        )
        if semantic_input_digest in semantic_input_digests:
            raise ProtocolError(
                "duplicate semantic evaluator input: "
                f"{semantic_input_digest}"
            )
        semantic_input_digests.add(semantic_input_digest)
        accepted.append(
            {
                "episode_id": episode_id,
                "group_id": group_id,
                "source_fingerprint": source_fingerprint,
                "source_class": source_class,
                "evaluated_at": evaluated_at,
                "goals_state": goals_state,
                "event_inbox_state": event_inbox_state,
            }
        )

    declared = manifest["data_class"]
    if declared == "real_project" and classes - {"real_project"}:
        raise ProtocolError(
            "real_project manifest contains a synthetic episode"
        )
    if declared == "synthetic" and classes - {"synthetic"}:
        raise ProtocolError(
            "synthetic manifest contains a real_project episode"
        )
    if declared == "mixed" and classes and classes != {
        "real_project",
        "synthetic",
    }:
        raise ProtocolError(
            "mixed manifest must contain both source classes"
        )
    return accepted, actual_sha256


def _scope(value: Any, *, location: str) -> dict[str, str]:
    scope = _exact_keys(
        value,
        required=set(ProjectGuardianEvaluator.SCOPE_FIELDS),
        location=location,
    )
    return {
        key: _text(
            scope[key],
            location=f"{location}.{key}",
            limit=240,
        )
        for key in ProjectGuardianEvaluator.SCOPE_FIELDS
    }


def _canonical_goals_state(
    value: Any,
    *,
    location: str,
) -> dict[str, Any]:
    state = _exact_keys(
        value,
        required={"goals"},
        location=location,
    )
    raw_goals = state["goals"]
    if not isinstance(raw_goals, list):
        raise ProtocolError(f"{location}.goals must be a list")
    goals: list[dict[str, Any]] = []
    goal_ids: set[str] = set()
    for index, raw_goal in enumerate(raw_goals):
        goal_location = f"{location}.goals[{index}]"
        goal = _exact_keys(
            raw_goal,
            required={
                "schema_version",
                "goal_id",
                "kind",
                "status",
                "user_id",
                "revision",
                "state_revision",
                "scope",
                "target_sha",
                "active_from",
                "active_until",
                "source",
            },
            location=goal_location,
        )
        if goal["schema_version"] != ProjectGuardianEvaluator.GOAL_SCHEMA:
            raise ProtocolError(
                f"unsupported Goal schema at {goal_location}"
            )
        if goal["source"] != ProjectGuardianEvaluator.GOAL_SOURCE:
            raise ProtocolError(
                f"unsupported Goal source at {goal_location}"
            )
        if goal["kind"] != ProjectGuardianEvaluator.GOAL_KIND:
            raise ProtocolError(
                f"unsupported Goal kind at {goal_location}"
            )
        status_value = _text(
            goal["status"],
            location=f"{goal_location}.status",
            limit=80,
        )
        if status_value not in {"active", "paused", "completed"}:
            raise ProtocolError(
                f"unsupported Goal status at {goal_location}"
            )
        target_sha = _text(
            goal["target_sha"],
            location=f"{goal_location}.target_sha",
            limit=64,
        ).lower()
        if (
            len(target_sha) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in target_sha)
        ):
            raise ProtocolError(
                f"{goal_location}.target_sha must be a full hexadecimal SHA"
            )
        active_from = _aware_time(
            goal["active_from"],
            location=f"{goal_location}.active_from",
        )
        active_until = _aware_time(
            goal["active_until"],
            location=f"{goal_location}.active_until",
        )
        if datetime.fromisoformat(active_until) < datetime.fromisoformat(
            active_from
        ):
            raise ProtocolError(
                f"{goal_location}.active_until must not precede active_from"
            )
        goal_id = _text(
            goal["goal_id"],
            location=f"{goal_location}.goal_id",
            limit=240,
        )
        if goal_id in goal_ids:
            raise ProtocolError(
                f"duplicate controlled Goal goal_id at {location}: {goal_id}"
            )
        goal_ids.add(goal_id)
        goals.append(
            {
                "schema_version": goal["schema_version"],
                "goal_id": goal_id,
                "kind": goal["kind"],
                "status": status_value,
                "user_id": _text(
                    goal["user_id"],
                    location=f"{goal_location}.user_id",
                    limit=240,
                ),
                "revision": _text(
                    goal["revision"],
                    location=f"{goal_location}.revision",
                    limit=120,
                ),
                "state_revision": _positive_int(
                    goal["state_revision"],
                    location=f"{goal_location}.state_revision",
                ),
                "scope": _scope(
                    goal["scope"],
                    location=f"{goal_location}.scope",
                ),
                "target_sha": target_sha,
                "active_from": active_from,
                "active_until": active_until,
                "source": goal["source"],
            }
        )
    goals.sort(key=_canonical_bytes)
    return {"goals": goals}


def _canonical_evidence_refs(
    value: Any,
    *,
    location: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ProtocolError(f"{location} must be a list")
    if len(value) > 32:
        raise ProtocolError(f"{location} exceeds 32 evidence references")
    selected: list[dict[str, Any]] = []
    for index, raw_ref in enumerate(value):
        ref_location = f"{location}[{index}]"
        ref = _exact_keys(
            raw_ref,
            required={"ref_id", "source", "is_fact"},
            location=ref_location,
        )
        if not isinstance(ref["is_fact"], bool):
            raise ProtocolError(f"{ref_location}.is_fact must be boolean")
        selected.append(
            {
                "ref_id": _text(
                    ref["ref_id"],
                    location=f"{ref_location}.ref_id",
                    limit=240,
                ),
                "source": _text(
                    ref["source"],
                    location=f"{ref_location}.source",
                    limit=120,
                ),
                "is_fact": ref["is_fact"],
            }
        )
    selected.sort(key=_canonical_bytes)
    if len({_json_sha256(item) for item in selected}) != len(selected):
        raise ProtocolError(f"{location} must not contain duplicates")
    return selected


def _canonical_signal_record(
    value: Any,
    *,
    location: str,
) -> dict[str, Any]:
    record = _exact_keys(
        value,
        required={"status", "envelope"},
        location=location,
    )
    envelope_location = f"{location}.envelope"
    envelope = _exact_keys(
        record["envelope"],
        required={
            "type",
            "event_id",
            "timestamp",
            "occurred_at",
            "source",
            "payload",
            "evidence_refs",
            "privacy_scope",
        },
        location=envelope_location,
    )
    source = _exact_keys(
        envelope["source"],
        required={"channel", "user_id", "session_id"},
        location=f"{envelope_location}.source",
    )
    payload = _exact_keys(
        envelope["payload"],
        required={"schema_version", "project_guardian_signal"},
        location=f"{envelope_location}.payload",
    )
    signal_location = f"{envelope_location}.payload.project_guardian_signal"
    signal = _exact_keys(
        payload["project_guardian_signal"],
        required={
            "kind",
            "state",
            "source_component",
            "provenance_root",
            "evidence_id",
            "goal_id",
            "goal_revision",
            "scope",
            "valid_until",
            "producer_attestation",
        },
        location=signal_location,
    )
    attestation = _exact_keys(
        signal["producer_attestation"],
        required={
            "schema_version",
            "producer_id",
            "trust_class",
            "admission_source",
            "receipt_id",
        },
        location=f"{signal_location}.producer_attestation",
    )
    kind = _text(
        signal["kind"],
        location=f"{signal_location}.kind",
        limit=120,
    )
    if kind not in ProjectGuardianEvaluator.SIGNAL_COMPONENTS:
        raise ProtocolError(f"unsupported signal kind at {signal_location}")
    state = _text(
        signal["state"],
        location=f"{signal_location}.state",
        limit=40,
    )
    if state not in ProjectGuardianEvaluator.SIGNAL_STATES:
        raise ProtocolError(f"unsupported signal state at {signal_location}")
    record_status = _text(
        record["status"],
        location=f"{location}.status",
        limit=80,
    )
    if record_status != "recorded":
        raise ProtocolError(
            f"{location}.status must be recorded for signal frontier input"
        )
    event_type = _text(
        envelope["type"],
        location=f"{envelope_location}.type",
        limit=80,
    )
    if event_type != "observation":
        raise ProtocolError(
            f"{envelope_location}.type must be observation"
        )
    timestamp = _aware_time(
        envelope["timestamp"],
        location=f"{envelope_location}.timestamp",
    )
    occurred_at = _aware_time(
        envelope["occurred_at"],
        location=f"{envelope_location}.occurred_at",
    )
    if timestamp != occurred_at:
        raise ProtocolError(
            f"{envelope_location}.timestamp must equal occurred_at"
        )
    evidence_refs = _canonical_evidence_refs(
        envelope["evidence_refs"],
        location=f"{envelope_location}.evidence_refs",
    )
    if len(evidence_refs) != 1:
        raise ProtocolError(
            f"{envelope_location}.evidence_refs must contain exactly one item"
        )
    return {
        "status": record_status,
        "envelope": {
            "type": event_type,
            "event_id": _text(
                envelope["event_id"],
                location=f"{envelope_location}.event_id",
                limit=240,
            ),
            "timestamp": timestamp,
            "occurred_at": occurred_at,
            "source": {
                "channel": _text(
                    source["channel"],
                    location=f"{envelope_location}.source.channel",
                    limit=120,
                ),
                "user_id": _text(
                    source["user_id"],
                    location=f"{envelope_location}.source.user_id",
                    limit=240,
                ),
                "session_id": _text(
                    source["session_id"],
                    location=f"{envelope_location}.source.session_id",
                    limit=240,
                ),
            },
            "payload": {
                "schema_version": _text(
                    payload["schema_version"],
                    location=f"{envelope_location}.payload.schema_version",
                    limit=120,
                ),
                "project_guardian_signal": {
                    "kind": kind,
                    "state": state,
                    "source_component": _text(
                        signal["source_component"],
                        location=f"{signal_location}.source_component",
                        limit=120,
                    ),
                    "provenance_root": _text(
                        signal["provenance_root"],
                        location=f"{signal_location}.provenance_root",
                        limit=240,
                    ),
                    "evidence_id": _text(
                        signal["evidence_id"],
                        location=f"{signal_location}.evidence_id",
                        limit=240,
                    ),
                    "goal_id": _text(
                        signal["goal_id"],
                        location=f"{signal_location}.goal_id",
                        limit=240,
                    ),
                    "goal_revision": _text(
                        signal["goal_revision"],
                        location=f"{signal_location}.goal_revision",
                        limit=120,
                    ),
                    "scope": _scope(
                        signal["scope"],
                        location=f"{signal_location}.scope",
                    ),
                    "valid_until": _aware_time(
                        signal["valid_until"],
                        location=f"{signal_location}.valid_until",
                    ),
                    "producer_attestation": {
                        "schema_version": _text(
                            attestation["schema_version"],
                            location=(
                                f"{signal_location}.producer_attestation"
                                ".schema_version"
                            ),
                            limit=120,
                        ),
                        "producer_id": _text(
                            attestation["producer_id"],
                            location=(
                                f"{signal_location}.producer_attestation"
                                ".producer_id"
                            ),
                            limit=200,
                        ),
                        "trust_class": _text(
                            attestation["trust_class"],
                            location=(
                                f"{signal_location}.producer_attestation"
                                ".trust_class"
                            ),
                            limit=120,
                        ),
                        "admission_source": _text(
                            attestation["admission_source"],
                            location=(
                                f"{signal_location}.producer_attestation"
                                ".admission_source"
                            ),
                            limit=120,
                        ),
                        "receipt_id": _text(
                            attestation["receipt_id"],
                            location=(
                                f"{signal_location}.producer_attestation"
                                ".receipt_id"
                            ),
                            limit=240,
                        ),
                    },
                },
            },
            "evidence_refs": evidence_refs,
            "privacy_scope": _text(
                envelope["privacy_scope"],
                location=f"{envelope_location}.privacy_scope",
                limit=80,
            ),
        },
    }


def _canonical_event_inbox_state(
    value: Any,
    *,
    location: str,
) -> dict[str, Any]:
    state = _exact_keys(
        value,
        required={"schema_version", "events"},
        location=location,
    )
    if (
        state["schema_version"]
        != "veyra.project_guardian_signal_frontier.v1"
    ):
        raise ProtocolError(f"unsupported signal frontier schema at {location}")
    raw_events = state["events"]
    if not isinstance(raw_events, dict):
        raise ProtocolError(f"{location}.events must be an object")
    events: dict[str, dict[str, Any]] = {}
    for raw_key, raw_record in raw_events.items():
        _text(
            raw_key,
            location=f"{location}.events key",
            limit=240,
        )
        record = _canonical_signal_record(
            raw_record,
            location=f"{location}.events.{raw_key}",
        )
        event_id = str(record["envelope"]["event_id"])
        if event_id in events:
            raise ProtocolError(
                f"duplicate signal event_id at {location}: {event_id}"
            )
        # The evaluator ignores the frontier dictionary key. Re-keying by the
        # immutable event identity prevents aliases from inflating support.
        events[event_id] = record
    return {
        "schema_version": state["schema_version"],
        "events": events,
    }


def _semantic_evaluator_input_digest(
    *,
    evaluated_at: str,
    goals_state: dict[str, Any],
    event_inbox_state: dict[str, Any],
) -> str:
    """Hash only decision-relevant evaluator semantics.

    Validation-only values such as Goal state revisions, target SHAs, signal
    receipts, and exact validity deadlines must not let one underlying episode
    masquerade as independent support. Their evaluator-visible relationships
    remain represented by active-Goal admission, freshness, and Goal-window
    membership. Goals the evaluator skips are excluded entirely.
    """

    evaluator = ProjectGuardianEvaluator()
    now = datetime.fromisoformat(evaluated_at)
    signals, diagnostics = evaluator._signals(
        copy.deepcopy(event_inbox_state),
        now,
    )
    raw_events = event_inbox_state["events"]
    if (
        diagnostics["rejected_signal_count"] != 0
        or diagnostics["deduplicated_signal_count"] != 0
        or len(signals) != len(raw_events)
    ):
        raise ProtocolError(
            "signal frontier must contain exactly one evaluator-accepted "
            "signal per event"
        )
    frontier_identities: set[tuple[str, ...]] = set()
    for signal in signals:
        identity = (
            signal["user_id"],
            signal["goal_id"],
            signal["goal_revision"],
            *(signal["scope"][key] for key in evaluator.SCOPE_FIELDS),
            signal["kind"],
        )
        if identity in frontier_identities:
            raise ProtocolError(
                "signal frontier must contain at most one current record per "
                "Goal scope and signal kind"
            )
        frontier_identities.add(identity)

    active_goals, _ = evaluator._active_release_goals(
        copy.deepcopy(goals_state),
        now,
    )
    goal_records: list[
        tuple[dict[str, Any], dict[str, Any], str]
    ] = []
    for goal in active_goals:
        projection = {
            "goal_id": goal["goal_id"],
            "user_id": goal["user_id"],
            "revision": goal["revision"],
            "scope": copy.deepcopy(goal["scope"]),
        }
        goal_records.append(
            (goal, projection, _json_sha256(projection))
        )

    projected_signals: list[tuple[dict[str, Any], datetime]] = []
    matched_goal_record_indexes: set[int] = set()
    for signal in signals:
        matching_goal_windows: list[str] = []
        for goal_index, (
            goal,
            projection,
            projection_digest,
        ) in enumerate(goal_records):
            if (
                signal["user_id"] == goal["user_id"]
                and signal["goal_id"] == goal["goal_id"]
                and signal["goal_revision"] == goal["revision"]
                and evaluator._same_scope(signal["scope"], goal["scope"])
                and goal["active_from"]
                <= signal["occurred_at"]
                <= goal["active_until"]
            ):
                matching_goal_windows.append(projection_digest)
                matched_goal_record_indexes.add(goal_index)
        matching_goal_windows.sort()
        if not matching_goal_windows:
            continue
        projected_signals.append(
            ({
                "kind": signal["kind"],
                "state": signal["state"],
                "fresh": bool(signal["fresh"]),
                "source_component": signal["source_component"],
                "provenance_lineage": signal["provenance_lineage"],
                "producer_id": signal["producer_id"],
                "evidence_id": signal["evidence_id"],
                "goal_id": signal["goal_id"],
                "goal_revision": signal["goal_revision"],
                "user_id": signal["user_id"],
                "session_id": signal["session_id"],
                "event_id": signal["event_id"],
                "scope": copy.deepcopy(signal["scope"]),
                "matching_goal_windows": matching_goal_windows,
            }, signal["occurred_at"])
        )
    signal_projection: list[dict[str, Any]] = []
    if projected_signals:
        first_occurred_at = min(
            occurred_at
            for _, occurred_at in projected_signals
        )
        for projection, occurred_at in projected_signals:
            occurred_offset = occurred_at - first_occurred_at
            projection["occurred_offset_microseconds"] = (
                occurred_offset.days * 86_400_000_000
                + occurred_offset.seconds * 1_000_000
                + occurred_offset.microseconds
            )
            signal_projection.append(projection)
    signal_projection.sort(key=_canonical_bytes)
    goal_projection = [
        projection
        for index, (_, projection, _) in enumerate(goal_records)
        if index in matched_goal_record_indexes
    ]
    goal_projection.sort(key=_canonical_bytes)
    return _json_sha256(
        {
            "goals": goal_projection,
            "signals": signal_projection,
        }
    )


def _association(value: Any, *, location: str) -> dict[str, Any]:
    association = _exact_keys(
        value,
        required={
            "candidate_kind",
            "user_id",
            "goal_id",
            "goal_revision",
            "scope",
        },
        location=location,
    )
    if association["candidate_kind"] != ProjectGuardianEvaluator.CANDIDATE_KIND:
        raise ProtocolError(f"unsupported candidate kind at {location}")
    return {
        "candidate_kind": association["candidate_kind"],
        "user_id": _text(
            association["user_id"],
            location=f"{location}.user_id",
            limit=240,
        ),
        "goal_id": _text(
            association["goal_id"],
            location=f"{location}.goal_id",
            limit=240,
        ),
        "goal_revision": _text(
            association["goal_revision"],
            location=f"{location}.goal_revision",
            limit=120,
        ),
        "scope": _scope(
            association["scope"],
            location=f"{location}.scope",
        ),
    }


def _association_key(value: dict[str, Any]) -> str:
    return _canonical_bytes(value).decode("utf-8")


def _string_set(
    value: Any,
    *,
    location: str,
    min_items: int = 0,
) -> list[str]:
    if not isinstance(value, list):
        raise ProtocolError(f"{location} must be a list")
    selected = [
        _text(item, location=f"{location}[{index}]", limit=300)
        for index, item in enumerate(value)
    ]
    if len(selected) < min_items:
        raise ProtocolError(
            f"{location} must contain at least {min_items} item(s)"
        )
    if len(set(selected)) != len(selected):
        raise ProtocolError(f"{location} must not contain duplicates")
    return sorted(selected)


def _prediction_from_candidate(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ProtocolError("evaluator candidate must be an object")
    locks = {
        "agent_invoked": False,
        "shadow_only": True,
        "notification_allowed": False,
        "execution_allowed": False,
        "interrupt_eligible": False,
    }
    if any(candidate.get(key) is not expected for key, expected in locks.items()):
        raise ProtocolError("evaluator candidate violated read-only authority locks")
    association = _association(
        {
            "candidate_kind": candidate.get("candidate_kind"),
            "user_id": candidate.get("user_id"),
            "goal_id": candidate.get("goal_id"),
            "goal_revision": candidate.get("goal_revision"),
            "scope": candidate.get("scope"),
        },
        location="evaluator_candidate.association",
    )
    evidence_refs = candidate.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        raise ProtocolError("evaluator candidate evidence_refs must be a list")
    evidence_ref_ids: list[str] = []
    for index, ref in enumerate(evidence_refs):
        if not isinstance(ref, dict):
            raise ProtocolError(
                f"evaluator candidate evidence_refs[{index}] must be an object"
            )
        if ref.get("is_fact") is not False:
            raise ProtocolError(
                "replay predictions may expose references, never factual claims"
            )
        evidence_ref_ids.append(
            _text(
                ref.get("ref_id"),
                location=f"evaluator_candidate.evidence_refs[{index}].ref_id",
                limit=300,
            )
        )
    evidence_ref_ids = _string_set(
        evidence_ref_ids,
        location="evaluator_candidate.evidence_ref_ids",
        min_items=2,
    )
    review_payload = {
        "why_now": copy.deepcopy(candidate.get("why_now")),
        "unknowns": copy.deepcopy(candidate.get("unknowns")),
        "candidate_advice": copy.deepcopy(candidate.get("candidate_advice")),
    }
    if (
        not isinstance(review_payload["why_now"], dict)
        or not isinstance(review_payload["unknowns"], list)
        or not isinstance(review_payload["candidate_advice"], dict)
    ):
        raise ProtocolError("candidate review payload is incomplete")
    _reject_leakage_fields(
        review_payload,
        location="evaluator_candidate.review_payload",
        reject_labels=False,
    )
    return {
        "candidate_id": _text(
            candidate.get("candidate_id"),
            location="evaluator_candidate.candidate_id",
            limit=240,
        ),
        "candidate_revision": _text(
            candidate.get("candidate_revision"),
            location="evaluator_candidate.candidate_revision",
            limit=240,
        ),
        "association": association,
        "evidence_ref_ids": evidence_ref_ids,
        "review_payload": review_payload,
        **locks,
    }


def _evaluate_episode_predictions(
    episodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    evaluator = ProjectGuardianEvaluator()
    episode_predictions: list[dict[str, Any]] = []
    prediction_count = 0
    for episode in episodes:
        result = evaluator.evaluate(
            goals_state=copy.deepcopy(episode["goals_state"]),
            event_inbox_state=copy.deepcopy(episode["event_inbox_state"]),
            now=episode["evaluated_at"],
        )
        raw_candidates = result.get("candidates")
        if not isinstance(raw_candidates, list):
            raise ProtocolError("evaluator returned invalid candidates")
        predictions = [
            _prediction_from_candidate(candidate)
            for candidate in raw_candidates
        ]
        predictions.sort(
            key=lambda item: _association_key(item["association"])
        )
        keys = [
            _association_key(item["association"])
            for item in predictions
        ]
        if len(keys) != len(set(keys)):
            raise ProtocolError(
                f"duplicate prediction association in {episode['episode_id']}"
            )
        prediction_count += len(predictions)
        episode_predictions.append(
            {
                "episode_id": episode["episode_id"],
                "predictions": predictions,
            }
        )
    return episode_predictions, prediction_count


def predict(
    *,
    manifest_path: Path | str,
    episodes_path: Path | str,
    output_path: Path | str,
) -> dict[str, Any]:
    """Create a frozen prediction artifact without accepting or reading labels."""

    manifest, manifest_sha256 = _manifest(manifest_path)
    episodes, episodes_sha256 = _episodes(manifest, episodes_path)
    episode_predictions, prediction_count = _evaluate_episode_predictions(
        episodes
    )
    artifact: dict[str, Any] = {
        "schema_version": PREDICTIONS_SCHEMA,
        "dataset_id": manifest["dataset_id"],
        "manifest_sha256": manifest_sha256,
        "episodes_sha256": episodes_sha256,
        "evaluator_contract": ProjectGuardianEvaluator.CANDIDATE_SCHEMA,
        "evaluator_ruleset": (
            ProjectGuardianEvaluator.EVALUATOR_RULESET_VERSION
        ),
        "episode_count": len(episode_predictions),
        "prediction_count": prediction_count,
        "episodes": episode_predictions,
    }
    artifact["prediction_set_sha256"] = _json_sha256(artifact)
    _write_frozen_json(output_path, artifact)
    return artifact


def _predictions(
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
    episodes_sha256: str,
    episode_ids: set[str],
    path: Path | str,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    value = _exact_keys(
        _load_json(path, artifact="predictions"),
        required={
            "schema_version",
            "dataset_id",
            "manifest_sha256",
            "episodes_sha256",
            "evaluator_contract",
            "evaluator_ruleset",
            "episode_count",
            "prediction_count",
            "episodes",
            "prediction_set_sha256",
        },
        location="predictions",
    )
    if value["schema_version"] != PREDICTIONS_SCHEMA:
        raise ProtocolError("unsupported predictions schema")
    if value["dataset_id"] != manifest["dataset_id"]:
        raise ProtocolError("predictions dataset_id does not match manifest")
    if value["manifest_sha256"] != manifest_sha256:
        raise ProtocolError("predictions are not bound to this manifest")
    if value["episodes_sha256"] != episodes_sha256:
        raise ProtocolError("predictions are not bound to these episodes")
    if value["evaluator_contract"] != ProjectGuardianEvaluator.CANDIDATE_SCHEMA:
        raise ProtocolError("predictions evaluator contract does not match")
    if (
        value["evaluator_ruleset"]
        != ProjectGuardianEvaluator.EVALUATOR_RULESET_VERSION
    ):
        raise ProtocolError("predictions evaluator ruleset does not match")
    frozen_sha256 = _sha256_text(
        value["prediction_set_sha256"],
        location="predictions.prediction_set_sha256",
    )
    unsigned = copy.deepcopy(value)
    unsigned.pop("prediction_set_sha256")
    if _json_sha256(unsigned) != frozen_sha256:
        raise ProtocolError("frozen predictions SHA-256 mismatch")
    raw_episodes = value["episodes"]
    if not isinstance(raw_episodes, list):
        raise ProtocolError("predictions.episodes must be a list")
    if value["episode_count"] != len(raw_episodes):
        raise ProtocolError("predictions episode_count mismatch")

    selected: dict[str, dict[str, dict[str, Any]]] = {}
    total = 0
    for index, raw_episode in enumerate(raw_episodes):
        episode = _exact_keys(
            raw_episode,
            required={"episode_id", "predictions"},
            location=f"predictions.episodes[{index}]",
        )
        episode_id = _text(
            episode["episode_id"],
            location=f"predictions.episodes[{index}].episode_id",
            limit=200,
        )
        if episode_id in selected:
            raise ProtocolError(
                f"duplicate predictions episode_id: {episode_id}"
            )
        raw_items = episode["predictions"]
        if not isinstance(raw_items, list):
            raise ProtocolError(
                f"predictions for {episode_id} must be a list"
            )
        by_association: dict[str, dict[str, Any]] = {}
        for item_index, raw_item in enumerate(raw_items):
            item = _exact_keys(
                raw_item,
                required={
                    "candidate_id",
                    "candidate_revision",
                    "association",
                    "evidence_ref_ids",
                    "review_payload",
                    "agent_invoked",
                    "shadow_only",
                    "notification_allowed",
                    "execution_allowed",
                    "interrupt_eligible",
                },
                location=(
                    f"predictions.episodes[{index}].predictions[{item_index}]"
                ),
            )
            locks = {
                "agent_invoked": False,
                "shadow_only": True,
                "notification_allowed": False,
                "execution_allowed": False,
                "interrupt_eligible": False,
            }
            if any(item.get(key) is not expected for key, expected in locks.items()):
                raise ProtocolError("frozen prediction authority lock mismatch")
            association = _association(
                item["association"],
                location=(
                    f"predictions.episodes[{index}]"
                    f".predictions[{item_index}].association"
                ),
            )
            normalized = {
                "candidate_id": _text(
                    item["candidate_id"],
                    location="prediction.candidate_id",
                    limit=240,
                ),
                "candidate_revision": _text(
                    item["candidate_revision"],
                    location="prediction.candidate_revision",
                    limit=240,
                ),
                "association": association,
                "evidence_ref_ids": _string_set(
                    item["evidence_ref_ids"],
                    location="prediction.evidence_ref_ids",
                    min_items=2,
                ),
                "review_payload": copy.deepcopy(item["review_payload"]),
                **locks,
            }
            _reject_leakage_fields(
                normalized["review_payload"],
                location="prediction.review_payload",
                reject_labels=False,
            )
            key = _association_key(association)
            if key in by_association:
                raise ProtocolError(
                    f"duplicate prediction association in {episode_id}"
                )
            by_association[key] = normalized
            total += 1
        selected[episode_id] = by_association
    if set(selected) != episode_ids:
        raise ProtocolError(
            "predictions must cover every episode exactly once"
        )
    if value["prediction_count"] != total:
        raise ProtocolError("predictions prediction_count mismatch")
    return selected, value


def _human_review(
    value: Any,
    *,
    location: str,
) -> tuple[str, dict[str, Any]]:
    review = _exact_keys(
        value,
        required={
            "association",
            "candidate_revision",
            "ratings",
        },
        optional={"adjudication"},
        location=location,
    )
    association = _association(
        review["association"],
        location=f"{location}.association",
    )
    candidate_revision = _text(
        review["candidate_revision"],
        location=f"{location}.candidate_revision",
        limit=240,
    )
    ratings = review["ratings"]
    if not isinstance(ratings, list):
        raise ProtocolError(f"{location}.ratings must be a list")
    selected_ratings: list[dict[str, Any]] = []
    rater_ids: set[str] = set()
    for index, raw_rating in enumerate(ratings):
        rating = _exact_keys(
            raw_rating,
            required={"rater_id", "useful"},
            location=f"{location}.ratings[{index}]",
        )
        rater_id = _text(
            rating["rater_id"],
            location=f"{location}.ratings[{index}].rater_id",
            limit=120,
        )
        if rater_id in rater_ids:
            raise ProtocolError(f"duplicate rater_id at {location}")
        if not isinstance(rating["useful"], bool):
            raise ProtocolError(
                f"{location}.ratings[{index}].useful must be boolean"
            )
        rater_ids.add(rater_id)
        selected_ratings.append(
            {"rater_id": rater_id, "useful": rating["useful"]}
        )

    adjudication_value = review.get("adjudication")
    adjudication: dict[str, Any] | None = None
    if adjudication_value is not None:
        raw_adjudication = _exact_keys(
            adjudication_value,
            required={"adjudicator_id", "useful", "reason_code"},
            location=f"{location}.adjudication",
        )
        adjudicator_id = _text(
            raw_adjudication["adjudicator_id"],
            location=f"{location}.adjudication.adjudicator_id",
            limit=120,
        )
        if adjudicator_id in rater_ids:
            raise ProtocolError(
                f"adjudicator must be independent at {location}"
            )
        if not isinstance(raw_adjudication["useful"], bool):
            raise ProtocolError(
                f"{location}.adjudication.useful must be boolean"
            )
        adjudication = {
            "adjudicator_id": adjudicator_id,
            "useful": raw_adjudication["useful"],
            "reason_code": _text(
                raw_adjudication["reason_code"],
                location=f"{location}.adjudication.reason_code",
                limit=120,
            ),
        }

    votes = {item["useful"] for item in selected_ratings}
    if adjudication is None:
        if len(selected_ratings) < 2:
            raise ProtocolError(
                f"{location} requires at least two independent raters "
                "or independent adjudication"
            )
        if len(votes) != 1:
            raise ProtocolError(
                f"disputed ratings require adjudication at {location}"
            )
        resolved = selected_ratings[0]["useful"]
        method = "dual_rater_consensus"
    else:
        if not selected_ratings:
            raise ProtocolError(
                f"{location} adjudication requires at least one primary rating"
            )
        resolved = adjudication["useful"]
        method = "independent_adjudication"

    return _association_key(association), {
        "association": association,
        "candidate_revision": candidate_revision,
        "ratings": selected_ratings,
        "adjudication": adjudication,
        "resolved_useful": resolved,
        "resolution_method": method,
    }


def _labels(
    *,
    manifest: dict[str, Any],
    episode_ids: set[str],
    path: Path | str,
) -> dict[str, dict[str, Any]]:
    selected_path = Path(path)
    loaded, actual_sha256 = _load_frozen_json(
        selected_path,
        artifact="labels",
    )
    if actual_sha256 != manifest["labels_sha256"]:
        raise ProtocolError("labels SHA-256 does not match manifest")
    value = _exact_keys(
        loaded,
        required={"schema_version", "dataset_id", "episodes"},
        location="labels",
    )
    if value["schema_version"] != LABELS_SCHEMA:
        raise ProtocolError("unsupported labels schema")
    if value["dataset_id"] != manifest["dataset_id"]:
        raise ProtocolError("labels dataset_id does not match manifest")
    _reject_leakage_fields(
        value,
        location="labels",
        reject_labels=False,
    )
    raw_episodes = value["episodes"]
    if not isinstance(raw_episodes, list):
        raise ProtocolError("labels.episodes must be a list")

    selected: dict[str, dict[str, Any]] = {}
    for index, raw_episode in enumerate(raw_episodes):
        episode = _exact_keys(
            raw_episode,
            required={
                "episode_id",
                "expected_candidates",
            },
            location=f"labels.episodes[{index}]",
        )
        episode_id = _text(
            episode["episode_id"],
            location=f"labels.episodes[{index}].episode_id",
            limit=200,
        )
        if episode_id in selected:
            raise ProtocolError(f"duplicate labels episode_id: {episode_id}")
        raw_expected = episode["expected_candidates"]
        if not isinstance(raw_expected, list):
            raise ProtocolError(
                f"expected_candidates for {episode_id} must be a list"
            )
        expected: dict[str, dict[str, Any]] = {}
        for expected_index, raw_candidate in enumerate(raw_expected):
            candidate = _exact_keys(
                raw_candidate,
                required={"association", "evidence_ref_ids"},
                location=(
                    f"labels.episodes[{index}]"
                    f".expected_candidates[{expected_index}]"
                ),
            )
            association = _association(
                candidate["association"],
                location=(
                    f"labels.episodes[{index}]"
                    f".expected_candidates[{expected_index}].association"
                ),
            )
            key = _association_key(association)
            if key in expected:
                raise ProtocolError(
                    f"duplicate expected association in {episode_id}"
                )
            expected[key] = {
                "association": association,
                "evidence_ref_ids": _string_set(
                    candidate["evidence_ref_ids"],
                    location=(
                        f"labels.episodes[{index}]"
                        f".expected_candidates[{expected_index}]"
                        ".evidence_ref_ids"
                    ),
                    min_items=2,
                ),
            }

        selected[episode_id] = {
            "expected": expected,
        }

    if set(selected) != episode_ids:
        raise ProtocolError("labels must cover every episode exactly once")
    return selected


def _reviews(
    *,
    manifest: dict[str, Any],
    episode_ids: set[str],
    predictions: dict[str, dict[str, dict[str, Any]]],
    prediction_set_sha256: str,
    path: Path | str,
) -> tuple[dict[str, dict[str, dict[str, Any]]], str]:
    selected_path = Path(path)
    loaded, actual_sha256 = _load_frozen_json(
        selected_path,
        artifact="reviews",
    )
    value = _exact_keys(
        loaded,
        required={
            "schema_version",
            "dataset_id",
            "prediction_set_sha256",
            "episodes",
        },
        location="reviews",
    )
    if value["schema_version"] != REVIEWS_SCHEMA:
        raise ProtocolError("unsupported reviews schema")
    if value["dataset_id"] != manifest["dataset_id"]:
        raise ProtocolError("reviews dataset_id does not match manifest")
    bound_prediction_sha256 = _sha256_text(
        value["prediction_set_sha256"],
        location="reviews.prediction_set_sha256",
    )
    if bound_prediction_sha256 != prediction_set_sha256:
        raise ProtocolError(
            "reviews are not bound to this frozen prediction set"
        )
    raw_episodes = value["episodes"]
    if not isinstance(raw_episodes, list):
        raise ProtocolError("reviews.episodes must be a list")

    selected: dict[str, dict[str, dict[str, Any]]] = {}
    for index, raw_episode in enumerate(raw_episodes):
        episode = _exact_keys(
            raw_episode,
            required={"episode_id", "human_reviews"},
            location=f"reviews.episodes[{index}]",
        )
        episode_id = _text(
            episode["episode_id"],
            location=f"reviews.episodes[{index}].episode_id",
            limit=200,
        )
        if episode_id in selected:
            raise ProtocolError(f"duplicate reviews episode_id: {episode_id}")
        raw_reviews = episode["human_reviews"]
        if not isinstance(raw_reviews, list):
            raise ProtocolError(
                f"human_reviews for {episode_id} must be a list"
            )
        reviews: dict[str, dict[str, Any]] = {}
        for review_index, raw_review in enumerate(raw_reviews):
            key, review = _human_review(
                raw_review,
                location=(
                    f"reviews.episodes[{index}]"
                    f".human_reviews[{review_index}]"
                ),
            )
            if key in reviews:
                raise ProtocolError(
                    f"duplicate human review association in {episode_id}"
                )
            predicted = predictions.get(episode_id, {}).get(key)
            if predicted is None:
                raise ProtocolError(
                    f"human review has no frozen prediction in {episode_id}"
                )
            if review["candidate_revision"] != predicted["candidate_revision"]:
                raise ProtocolError(
                    f"human review candidate revision mismatch in {episode_id}"
                )
            reviews[key] = review
        selected[episode_id] = reviews

    if set(selected) != episode_ids:
        raise ProtocolError("reviews must cover every episode exactly once")
    return selected, actual_sha256


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def score(
    *,
    manifest_path: Path | str,
    episodes_path: Path | str,
    predictions_path: Path | str,
    labels_path: Path | str,
    reviews_path: Path | str,
    output_path: Path | str,
) -> dict[str, Any]:
    """Score frozen predictions against separately loaded frozen labels."""

    manifest, manifest_sha256 = _manifest(manifest_path)
    episodes, episodes_sha256 = _episodes(manifest, episodes_path)
    episode_ids = {item["episode_id"] for item in episodes}
    predictions, prediction_artifact = _predictions(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        episodes_sha256=episodes_sha256,
        episode_ids=episode_ids,
        path=predictions_path,
    )
    recomputed_episodes, recomputed_count = (
        _evaluate_episode_predictions(episodes)
    )
    if (
        prediction_artifact["episodes"] != recomputed_episodes
        or prediction_artifact["prediction_count"] != recomputed_count
    ):
        raise ProtocolError(
            "frozen predictions do not match current evaluator output"
        )
    labels = _labels(
        manifest=manifest,
        episode_ids=episode_ids,
        path=labels_path,
    )
    reviews, reviews_sha256 = _reviews(
        manifest=manifest,
        episode_ids=episode_ids,
        predictions=predictions,
        prediction_set_sha256=prediction_artifact[
            "prediction_set_sha256"
        ],
        path=reviews_path,
    )

    tp = 0
    fp = 0
    fn = 0
    evidence_correct = 0
    evidence_incorrect = 0
    human_reviewed = 0
    human_useful = 0
    human_not_useful = 0
    missing_or_stale_reviews = 0
    positive_groups: set[str] = set()
    negative_groups: set[str] = set()
    human_reviewed_groups: set[str] = set()
    expected_candidate_count = 0
    predicted_candidate_count = 0
    per_episode: list[dict[str, Any]] = []
    episodes_by_id = {
        str(item["episode_id"]): item
        for item in episodes
    }

    for episode_id in sorted(episode_ids):
        group_id = str(episodes_by_id[episode_id]["group_id"])
        predicted = predictions[episode_id]
        expected = labels[episode_id]["expected"]
        episode_reviews = reviews[episode_id]
        predicted_keys = set(predicted)
        expected_keys = set(expected)
        matched = predicted_keys & expected_keys
        false = predicted_keys - expected_keys
        missed = expected_keys - predicted_keys
        tp += len(matched)
        fp += len(false)
        fn += len(missed)
        expected_candidate_count += len(expected)
        predicted_candidate_count += len(predicted)
        if expected:
            positive_groups.add(group_id)
        else:
            negative_groups.add(group_id)

        episode_evidence_correct = 0
        for key in matched:
            if (
                predicted[key]["evidence_ref_ids"]
                == expected[key]["evidence_ref_ids"]
            ):
                evidence_correct += 1
                episode_evidence_correct += 1
            else:
                evidence_incorrect += 1
        for key in predicted_keys:
            review = episode_reviews.get(key)
            if (
                review is None
                or review["candidate_revision"]
                != predicted[key]["candidate_revision"]
            ):
                missing_or_stale_reviews += 1
                continue
            human_reviewed += 1
            human_reviewed_groups.add(group_id)
            if review["resolved_useful"]:
                human_useful += 1
            else:
                human_not_useful += 1
        evidence_incorrect += len(false)
        per_episode.append(
            {
                "episode_id": episode_id,
                "group_id": group_id,
                "tp": len(matched),
                "fp": len(false),
                "fn": len(missed),
                "false_association_count": len(false),
                "evidence_correct": episode_evidence_correct,
                "evidence_incorrect": (
                    len(matched) - episode_evidence_correct + len(false)
                ),
            }
        )

    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    evidence_evaluated = evidence_correct + evidence_incorrect
    evidence_correctness = _ratio(
        evidence_correct,
        evidence_evaluated,
    )
    human_usefulness = _ratio(human_useful, human_reviewed)

    reasons: list[str] = []
    if not episodes:
        reasons.append("empty_dataset")
    if manifest["data_class"] != "real_project":
        reasons.append("corpus_not_real_project")
    if len(positive_groups) < MIN_POSITIVE_GROUPS:
        reasons.append("positive_group_support_below_minimum")
    if len(negative_groups) < MIN_NEGATIVE_GROUPS:
        reasons.append("negative_group_support_below_minimum")
    if precision is None:
        reasons.append("precision_undefined")
    elif precision < THRESHOLDS["precision"]:
        reasons.append("precision_below_threshold")
    if recall is None:
        reasons.append("recall_undefined")
    elif recall < THRESHOLDS["recall"]:
        reasons.append("recall_below_threshold")
    if fp > THRESHOLDS["false_association_count"]:
        reasons.append("false_association_detected")
    if evidence_correctness is None:
        reasons.append("evidence_correctness_undefined")
    elif evidence_correctness < THRESHOLDS["evidence_correctness"]:
        reasons.append("evidence_correctness_below_threshold")
    if missing_or_stale_reviews:
        reasons.append("human_review_coverage_incomplete")
    if len(human_reviewed_groups) < MIN_HUMAN_REVIEWED_GROUPS:
        reasons.append("human_reviewed_group_support_below_minimum")
    if human_usefulness is None:
        reasons.append("human_usefulness_undefined")
    elif human_usefulness < THRESHOLDS["human_usefulness"]:
        reasons.append("human_usefulness_below_threshold")
    reasons = list(dict.fromkeys(reasons))

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "dataset_id": manifest["dataset_id"],
        "validation_state": "ready" if not reasons else "not_ready",
        "data_class": manifest["data_class"],
        "evaluator_ruleset": (
            ProjectGuardianEvaluator.EVALUATOR_RULESET_VERSION
        ),
        "support": {
            "episode_count": len(episodes),
            "independent_group_count": len(episodes),
            "positive_group_count": len(positive_groups),
            "negative_group_count": len(negative_groups),
            "expected_candidate_count": expected_candidate_count,
            "predicted_candidate_count": predicted_candidate_count,
            "human_reviewed_prediction_count": human_reviewed,
            "human_reviewed_group_count": len(human_reviewed_groups),
        },
        "counts": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            # Conservative definition: every predicted association absent
            # from the frozen exact association labels is false association.
            "false_association_count": fp,
            "evidence_correct": evidence_correct,
            "evidence_incorrect": evidence_incorrect,
            "evidence_evaluated": evidence_evaluated,
            "human_useful": human_useful,
            "human_not_useful": human_not_useful,
            "human_review_missing_or_stale": missing_or_stale_reviews,
        },
        "metrics": {
            "precision": precision,
            "recall": recall,
            "evidence_correctness": evidence_correctness,
            "human_usefulness": human_usefulness,
        },
        "thresholds": {
            **THRESHOLDS,
            "minimum_positive_groups": MIN_POSITIVE_GROUPS,
            "minimum_negative_groups": MIN_NEGATIVE_GROUPS,
            "minimum_human_reviewed_groups": (
                MIN_HUMAN_REVIEWED_GROUPS
            ),
        },
        "not_ready_reasons": reasons,
        "artifact_integrity": {
            "manifest_sha256": manifest_sha256,
            "episodes_sha256": episodes_sha256,
            "labels_sha256": manifest["labels_sha256"],
            "reviews_sha256": reviews_sha256,
            "prediction_set_sha256": prediction_artifact[
                "prediction_set_sha256"
            ],
        },
        "episodes": per_episode,
        "authority": {
            "reads_runtime_state": False,
            "writes_runtime_state": False,
            "git_invoked": False,
            "agent_invoked": False,
            "notification_allowed": False,
            "tool_invoked": False,
        },
    }
    report["report_sha256"] = _json_sha256(report)
    _write_frozen_json(output_path, report)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run label-blind Project Guardian held-out replay and separate scoring."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    predict_parser = commands.add_parser(
        "predict",
        help="Generate frozen predictions without reading labels.",
    )
    predict_parser.add_argument("--manifest", required=True)
    predict_parser.add_argument("--episodes", required=True)
    predict_parser.add_argument("--output", required=True)

    score_parser = commands.add_parser(
        "score",
        help="Score frozen predictions against separately supplied labels.",
    )
    score_parser.add_argument("--manifest", required=True)
    score_parser.add_argument("--episodes", required=True)
    score_parser.add_argument("--predictions", required=True)
    score_parser.add_argument("--labels", required=True)
    score_parser.add_argument("--reviews", required=True)
    score_parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "predict":
            artifact = predict(
                manifest_path=args.manifest,
                episodes_path=args.episodes,
                output_path=args.output,
            )
            print(
                json.dumps(
                    {
                        "status": "predictions_frozen",
                        "dataset_id": artifact["dataset_id"],
                        "episode_count": artifact["episode_count"],
                        "prediction_count": artifact["prediction_count"],
                        "prediction_set_sha256": artifact[
                            "prediction_set_sha256"
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        report = score(
            manifest_path=args.manifest,
            episodes_path=args.episodes,
            predictions_path=args.predictions,
            labels_path=args.labels,
            reviews_path=args.reviews,
            output_path=args.output,
        )
        print(
            json.dumps(
                {
                    "status": report["validation_state"],
                    "dataset_id": report["dataset_id"],
                    "counts": report["counts"],
                    "metrics": report["metrics"],
                    "not_ready_reasons": report["not_ready_reasons"],
                    "report_sha256": report["report_sha256"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if report["validation_state"] == "ready" else 1
    except (OSError, ProtocolError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
