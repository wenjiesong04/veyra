"""Pure, bounded scheduling primitives for Belief refresh.

This module deliberately owns no state and performs no probe dispatch.  The
runtime supplies a virtual clock and uses the returned markers to persist a
small private scheduler ledger.  Claim truth remains in ``belief_state``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable

from awareness.belief_core import BeliefCore
from awareness.belief_economy import evaluate_refresh_schedule
from awareness.claim_schema import claim_identity_key


SCHEDULER_SCHEMA_VERSION = "veyra.state_refresh_state.v2"
MAX_CONSUMED_DUE = 512
MAX_UNREFRESHABLE = 512
MAX_CONFLICT_RETRY = 512
MAX_OWNER_OFFSETS = 256

_V2_STATE_KEYS = frozenset(
    {
        "schema_version",
        "cursor",
        "owner_offsets",
        "owner_count",
        "owner_scheduler_version",
        "consumed_due",
        "unrefreshable",
        "conflict_retry",
        "last_owner",
        "supported_count",
        "unsupported_count",
        "malformed_count",
        "selected_count",
        "refreshed_count",
        "failed_count",
        "skipped_count",
        "last_selected",
        "ledger_capacity",
        # WorldStateStore metadata and CAS revision.
        "source",
        "confidence",
        "ttl_seconds",
        "status",
        "updated_at",
        "_state_revision",
    }
)


def validate_refresh_state(state: Any) -> str | None:
    """Validate scheduler state before cursor/ledger mutation.

    Sparse legacy v1 state remains readable and is upgraded on the next
    successful write. Explicit v2 corruption fails closed instead of being
    silently normalized.
    """

    if not isinstance(state, dict) or state.get("_state_corrupt"):
        return "state_refresh_state_invalid"
    version = state.get("schema_version")
    if version not in {None, "veyra.state_refresh_state.v1", SCHEDULER_SCHEMA_VERSION}:
        return "state_refresh_schema_unsupported"
    if version != SCHEDULER_SCHEMA_VERSION:
        return None
    if set(state) - _V2_STATE_KEYS:
        return "state_refresh_v2_fields_unknown"
    required = {
        "schema_version",
        "cursor",
        "owner_offsets",
        "owner_count",
        "owner_scheduler_version",
        "consumed_due",
        "unrefreshable",
        "conflict_retry",
    }
    if not required.issubset(state):
        return "state_refresh_v2_fields_missing"
    if state.get("owner_scheduler_version") != 2:
        return "state_refresh_owner_scheduler_version_invalid"
    for key in ("consumed_due", "unrefreshable", "conflict_retry"):
        if key in state and not isinstance(state.get(key), list):
            return f"state_refresh_{key}_malformed"
    for key in ("cursor", "owner_count", "owner_scheduler_version"):
        value = state.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            return f"state_refresh_{key}_malformed"
    offsets = state.get("owner_offsets", {})
    if not isinstance(offsets, dict) or len(offsets) > MAX_OWNER_OFFSETS:
        return "state_refresh_owner_offsets_invalid"
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in offsets.values()):
        return "state_refresh_owner_offsets_invalid"
    return None


def schedule_claim(claim: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Return a deterministic, non-mutating scheduling projection."""

    return evaluate_refresh_schedule(claim, now=now)


def identity_marker(claim: dict[str, Any]) -> str:
    identity = claim_identity_key(claim)
    if identity is None:
        identity = ("invalid", str(claim.get("key") or claim.get("claim") or ""))
    encoded = json.dumps(list(identity), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def claim_digest(claim: dict[str, Any]) -> str:
    # Scheduler-only fields (schedule projection and raw admission binding)
    # are transport metadata, not durable claim truth.  Excluding them keeps
    # private markers stable across evaluator passes and prevents the nested
    # original row from becoming part of a receipt/ledger digest.
    private_fields = {
        "_refresh_schedule",
        "_refresh_original_claim",
        "_refresh_selected_projection",
        "_refresh_economy",
    }
    durable = {
        key: value
        for key, value in claim.items()
        if key not in private_fields
    }
    return BeliefCore.claim_projection_digest(durable)


def schedule_marker(claim: dict[str, Any]) -> str:
    """Exact identity + explicit producer schedule-generation marker.

    This marker intentionally excludes mutable observation timestamps and
    claim revisions. It is only for an explicit ``next_refresh_at`` cadence;
    legacy TTL lifecycle uses no consumed marker.
    """

    # A refresh changes observation timestamps and claim_revision. Those are
    # not schedule generations: carrying the producer's unchanged economy /
    # spec forward must keep the same due marker. Conversely, changing either
    # producer-owned schedule contract releases the marker.
    material_payload = {
        "economy": claim.get("economy"),
        "refresh_spec": claim.get("refresh_spec"),
    }
    material = f"{identity_marker(claim)}:{json.dumps(material_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def unrefreshable_marker(claim: dict[str, Any]) -> str:
    """Marker bound to the exact claim projection/revision.

    Unlike a consumed schedule generation, an archive must automatically
    release when a producer publishes a new observation or CAS revision even
    if its refresh spec remains unchanged.
    """

    material = f"{identity_marker(claim)}:{claim_digest(claim)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def conflict_retry_frontier(claim: dict[str, Any]) -> dict[str, Any] | None:
    """Project the meaningful evidence frontier of a durable conflict.

    A conflict retry is a governance attempt, not a second claim revision.
    ``claim_revision``, observation timestamps, history, and the ever-growing
    evidence-id list are therefore deliberately excluded.  Replaying the same
    probe result may append a new observation row, but it does not constitute a
    new frontier until its value or contradiction anchors change.

    The latest typed conflict observation is preferred.  Older rows written
    before ``conflict_observations`` existed fall back to the compact claim
    projection.  ``None`` means the state cannot establish a safe frontier;
    callers must not use such a projection to suppress a retry.
    """

    observations = claim.get("conflict_observations")
    latest: dict[str, Any] | None = None
    if isinstance(observations, list):
        for item in reversed(observations):
            if isinstance(item, dict):
                latest = item
                break

    source = latest if latest is not None else claim
    value_digest = source.get("value_digest")
    if value_digest is None:
        value_digest = claim.get("evidence_value_digest")
    if value_digest is not None and (
        not isinstance(value_digest, str) or not value_digest
    ):
        return None

    conflict_refs = source.get("conflict_refs")
    if conflict_refs is None:
        conflict_refs = claim.get("evidence_graph_conflict_refs")
    if conflict_refs is None:
        conflict_refs = []
    if not isinstance(conflict_refs, list) or any(
        not isinstance(item, str) or not item for item in conflict_refs
    ):
        return None
    conflict_refs = sorted(set(conflict_refs))
    graph_status = source.get("evidence_graph_status")
    if graph_status is None:
        graph_status = claim.get("evidence_graph_status")
    if graph_status is not None and (
        not isinstance(graph_status, str) or not graph_status
    ):
        return None
    # A frontier must contain at least one durable anchor.  A missing value
    # and no contradiction refs is an integrity ambiguity, not a suppressible
    # conflict.
    if value_digest is None and not conflict_refs:
        return None
    return {
        "value_digest": value_digest,
        "conflict_refs": conflict_refs,
        "evidence_graph_status": graph_status,
    }


def conflict_retry_key(claim: dict[str, Any]) -> str:
    """Stable private upsert slot for one exact owner/session/claim."""

    return identity_marker(claim)


def conflict_retry_generation(claim: dict[str, Any]) -> str | None:
    """Return the current retry generation, or ``None`` if unsafe to suppress.

    Producer-owned refresh specification and economy/cadence are part of the
    generation.  Thus a changed source contract or cadence releases a prior
    suppression even when the observed conflict value is unchanged.
    """

    frontier = conflict_retry_frontier(claim)
    if frontier is None:
        return None
    material = {
        "identity": conflict_retry_key(claim),
        "source": claim.get("source"),
        "refresh_spec": claim.get("refresh_spec"),
        "economy": claim.get("economy"),
        "evidence_frontier": frontier,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def conflict_retry_suppressed(
    claim: dict[str, Any],
    entries: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> bool:
    """Whether an exact conflict has an up-to-date private suppression.

    Legacy entries without a generation are not trusted to suppress a retry;
    they are compacted by the next successful write. ``retry_due_at`` is a
    server-owned explicit release signal.
    """

    if str(claim.get("status") or "") != "conflict":
        return False
    generation = conflict_retry_generation(claim)
    if generation is None:
        return False
    identity = conflict_retry_key(claim)
    for item in entries:
        if not isinstance(item, dict) or item.get("identity") != identity:
            continue
        if item.get("generation") != generation:
            continue
        retry_due_at = item.get("retry_due_at")
        if retry_due_at:
            if now is None:
                continue
            try:
                due = datetime.fromisoformat(str(retry_due_at).replace("Z", "+00:00"))
            except ValueError:
                # Malformed release metadata must fail closed: do not turn it
                # into either a silent suppression or an unbounded retry.
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if now.astimezone(timezone.utc) >= due.astimezone(timezone.utc):
                return False
        return True
    return False


def owner_key(claim: dict[str, Any]) -> str:
    raw_user = claim.get("user_id")
    raw_session = claim.get("session_id")
    user = str(raw_user) if raw_user is not None else ""
    session = str(raw_session) if raw_session is not None else ""
    return (
        f"{'present' if user else 'missing'}:{len(user)}:{user}|"
        f"{'present' if session else 'missing'}:{len(session)}:{session}"
    )


def tier_sort_key(claim: dict[str, Any]) -> tuple[Any, ...]:
    schedule = claim.get("_refresh_schedule") or {}
    raw_tier = schedule.get("tier_rank", 3)
    tier = int(raw_tier if raw_tier is not None else 3)
    value = schedule.get("economy_value")
    known = value is not None
    due = str(schedule.get("hard_deadline") or schedule.get("next_refresh_at") or "")
    age = int(schedule.get("age_seconds") or 0)
    return (
        tier,
        0 if known else 1,
        -(float(value) if known else 0.0),
        due,
        -age,
        owner_key(claim),
        str(claim.get("key") or claim.get("claim") or ""),
    )


def _priority_bucket(claim: dict[str, Any]) -> tuple[Any, ...]:
    """Return the non-owner priority bucket used for round-robin fairness."""

    schedule = claim.get("_refresh_schedule") or {}
    value = schedule.get("economy_value")
    known = value is not None
    due = str(schedule.get("hard_deadline") or schedule.get("next_refresh_at") or "")
    age = int(schedule.get("age_seconds") or 0)
    return (
        0 if known else 1,
        -(float(value) if known else 0.0),
        due,
        -age,
    )


def select_fair(
    claims: Iterable[dict[str, Any]],
    *,
    limit: int,
    owner_cursor: dict[str, int] | None = None,
    last_owner: str = "",
) -> tuple[list[dict[str, Any]], dict[str, int], str]:
    """Select by tier, then owner round-robin, without crossing tiers."""

    requested = max(0, int(limit))
    if requested == 0:
        return [], dict(owner_cursor or {}), last_owner
    ordered = sorted(claims, key=tier_sort_key)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for claim in ordered:
        marker = identity_marker(claim)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(claim)
    selected: list[dict[str, Any]] = []
    offsets = dict(owner_cursor or {})
    # The prior scheduler cursor is intentionally not a global priority. Each
    # tier gets its own owner round-robin, and each tier is split into priority
    # buckets first. Thus an unknown-economy owner cannot leapfrog a known
    # higher-value claim merely because it sorts into an earlier owner.
    for tier in (0, 1, 2):
        if len(selected) >= requested:
            break
        group = [
            claim
            for claim in unique
            if int(
                ((claim.get("_refresh_schedule") or {}).get("tier_rank", 3))
                if ((claim.get("_refresh_schedule") or {}).get("tier_rank", 3)) is not None
                else 3
            )
            == tier
        ]
        if not group:
            continue
        buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for claim in group:
            buckets.setdefault(_priority_bucket(claim), []).append(claim)
        bucket_order = sorted(buckets)
        priority_key = f"__priority_cursor__:t{tier}"
        raw_priority = offsets.get(priority_key, 0)
        try:
            priority_start = int(raw_priority)
        except (TypeError, ValueError):
            priority_start = 0
        priority_start %= len(bucket_order)
        for bucket_step in range(len(bucket_order)):
            if len(selected) >= requested:
                break
            bucket_index = (priority_start + bucket_step) % len(bucket_order)
            bucket = bucket_order[bucket_index]
            bucket_claims = buckets[bucket]
            owners: dict[str, list[dict[str, Any]]] = {}
            for claim in bucket_claims:
                owners.setdefault(owner_key(claim), []).append(claim)
            for values in owners.values():
                values.sort(key=tier_sort_key)
            owner_names = sorted(owners)
            bucket_token = json.dumps([tier, bucket], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            cursor_prefix = f"t{tier}:{hashlib.sha256(bucket_token.encode('utf-8')).hexdigest()[:16]}"
            cursor_names = {owner: f"{cursor_prefix}:{owner}" for owner in owner_names}
            previous_owner = str(last_owner or "")
            previous_local = previous_owner if previous_owner.startswith(cursor_prefix + ":") else ""
            if previous_local:
                previous_name = previous_local[len(cursor_prefix) + 1 :]
                start = (owner_names.index(previous_name) + 1) % len(owner_names) if previous_name in owner_names else 0
            else:
                start = 0
            # A bounded batch may take at most one unconsumed identity per
            # owner per round. Once every owner partition is exhausted, stop;
            # never wrap the same row into the same batch.
            positions = {
                owner: int(offsets.get(cursor_names[owner], 0) or 0) % len(values)
                for owner, values in owners.items()
            }
            consumed_in_bucket: set[str] = set()
            while len(selected) < requested and len(consumed_in_bucket) < len(bucket_claims):
                progressed = False
                for offset in range(len(owner_names)):
                    owner = owner_names[(start + offset) % len(owner_names)]
                    values = owners[owner]
                    index = positions[owner] % len(values)
                    selected_value = values[index]
                    marker = identity_marker(selected_value)
                    if marker in consumed_in_bucket:
                        continue
                    selected.append(selected_value)
                    consumed_in_bucket.add(marker)
                    positions[owner] = (index + 1) % len(values)
                    cursor_key = cursor_names[owner]
                    if cursor_key in offsets or len(offsets) < MAX_OWNER_OFFSETS:
                        offsets[cursor_key] = positions[owner]
                    last_owner = cursor_names[owner]
                    progressed = True
                    if len(selected) >= requested:
                        break
                if not progressed:
                    break
            # Keep known/value priority within a batch, but do not let a
            # permanently present high-value bucket starve lower buckets over
            # repeated bounded ticks. If this bucket was exhausted, advance;
            # otherwise resume it next tick before rotating further.
            if consumed_in_bucket and (
                priority_key in offsets or len(offsets) < MAX_OWNER_OFFSETS
            ):
                offsets[priority_key] = (bucket_index + 1) % len(bucket_order)
    return selected, offsets, last_owner


def consumed_entry(claim: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
    schedule = claim.get("_refresh_schedule") or {}
    # Legacy lifecycle/TTL claims have no explicit schedule generation. Their
    # fresh replacement naturally remains quiet until its next TTL boundary;
    # persisting a consumed marker here would suppress all future TTL refreshes.
    # Hard deadlines always pierce suppression.
    if not schedule.get("next_refresh_at") or schedule.get("hard_overdue"):
        return None
    return {
        "marker": schedule_marker(claim),
        "identity": identity_marker(claim),
        "claim_digest": claim_digest(claim),
        "due": str(schedule.get("hard_deadline") or schedule.get("next_refresh_at") or "lifecycle"),
        "consumed_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def unrefreshable_entry(claim: dict[str, Any], *, reason: str, now: datetime) -> dict[str, Any]:
    schedule = claim.get("_refresh_schedule") or {}
    marker = unrefreshable_marker(claim)
    timestamp = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "marker": marker,
        "identity": identity_marker(claim),
        "claim_digest": claim_digest(claim),
        "reason": reason,
        "first_seen": timestamp,
        "last_seen": timestamp,
        "hard_deadline": schedule.get("hard_deadline"),
    }


def conflict_retry_entry(
    claim: dict[str, Any],
    *,
    now: datetime,
    reason: str = "conflict_retry_suppressed",
) -> dict[str, Any]:
    """Bounded governance-attempt marker for cadence-less conflicts.

    ``marker`` is the exact identity plus producer contract/cadence and
    meaningful evidence frontier generation; ``identity`` is its upsert key.
    """

    timestamp = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    generation = conflict_retry_generation(claim)
    frontier = conflict_retry_frontier(claim)
    return {
        # Keep malformed frontiers observable; callers never suppress on None.
        "marker": generation or unrefreshable_marker(claim),
        "identity": conflict_retry_key(claim),
        "generation": generation,
        "evidence_frontier": frontier,
        "claim_digest": claim_digest(claim),
        "reason": reason,
        "attempted_at": timestamp,
    }


def _conflict_retry_slot(item: dict[str, Any]) -> str:
    """Return the stable identity slot for a current or legacy entry."""

    identity = item.get("identity")
    if isinstance(identity, str) and identity:
        return f"identity:{identity}"
    # Older private ledgers always carried a marker.  Keeping a marker-only
    # slot makes malformed legacy rows deterministic while validation still
    # rejects entries missing the marker itself.
    return f"marker:{item.get('marker')}"


def merge_conflict_retry_ledger(
    existing: Any,
    additions: Iterable[dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Upsert one latest-generation retry record per exact conflict identity.

    Legacy revision/digest churn is compacted on the next write. New
    identities obey the hard capacity bound without eviction.
    """

    if existing is None:
        current: list[dict[str, Any]] = []
    elif not isinstance(existing, list) or any(
        not isinstance(item, dict) or not item.get("marker") for item in existing
    ):
        raise ValueError("bounded ledger is malformed")
    else:
        current = [dict(item) for item in existing]
    if len(current) > limit:
        raise ValueError("bounded ledger exceeds capacity")

    by_slot: dict[str, dict[str, Any]] = {}
    # Last observation wins inside a slot; sorting makes replay deterministic.
    for item in sorted(
        current,
        key=lambda value: (
            str(value.get("attempted_at") or value.get("last_seen") or ""),
            str(value.get("marker") or ""),
        ),
    ):
        by_slot[_conflict_retry_slot(item)] = dict(item)

    blocked = 0
    for item in additions:
        if not isinstance(item, dict) or not item.get("marker"):
            continue
        slot = _conflict_retry_slot(item)
        previous = by_slot.get(slot)
        if previous is not None:
            merged = {**previous, **item}
            # Preserve the first attempt for diagnostics.
            merged["first_attempted_at"] = (
                previous.get("first_attempted_at")
                or previous.get("attempted_at")
                or item.get("attempted_at")
            )
            by_slot[slot] = merged
            continue
        if len(by_slot) >= limit:
            blocked += 1
            continue
        by_slot[slot] = dict(item)

    ordered = sorted(
        by_slot.values(),
        key=lambda item: (
            str(item.get("identity") or ""),
            str(item.get("marker") or ""),
        ),
    )
    return ordered, blocked


def merge_bounded_ledger(
    existing: Any,
    additions: Iterable[dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Merge by marker and report blocked additions instead of evicting."""

    if existing is None:
        current: list[dict[str, Any]] = []
    elif not isinstance(existing, list) or any(
        not isinstance(item, dict) or not item.get("marker") for item in existing
    ):
        raise ValueError("bounded ledger is malformed")
    else:
        current = [dict(item) for item in existing]
    if len(current) > limit:
        raise ValueError("bounded ledger exceeds capacity")
    by_marker = {str(item.get("marker")): dict(item) for item in current if item.get("marker")}
    blocked = 0
    for item in additions:
        marker = str(item.get("marker") or "")
        if not marker:
            continue
        previous = by_marker.get(marker)
        if previous is not None:
            merged = {**previous, **item}
            merged["first_seen"] = previous.get("first_seen") or item.get("first_seen")
            by_marker[marker] = merged
            continue
        if len(by_marker) >= limit:
            blocked += 1
            continue
        by_marker[marker] = dict(item)
    # Deterministic bounded storage. If a legacy/corrupt document is already
    # over capacity, retain it as-is and block new entries: silently evicting
    # a hard-deadline marker would turn scheduler pressure into false silence.
    ordered = sorted(
        by_marker.values(),
        key=lambda item: (
            0 if item.get("hard_deadline") else 1,
            str(item.get("last_seen") or ""),
            str(item.get("marker") or ""),
        ),
    )
    # Existing over-capacity input was rejected above. Additions are bounded
    # by construction and never silently evict a hard marker.
    return ordered, blocked


def validate_bounded_ledger(existing: Any, *, limit: int) -> str | None:
    """Validate a private ledger before any scheduler mutation.

    Over-capacity or malformed state is an integrity failure, not a reason to
    silently trim entries and continue.
    """

    if existing is None:
        return None
    if not isinstance(existing, list):
        return "ledger_not_list"
    if len(existing) > limit:
        return "ledger_over_capacity"
    markers: set[str] = set()
    for item in existing:
        if not isinstance(item, dict) or not isinstance(item.get("marker"), str) or not item.get("marker"):
            return "ledger_entry_malformed"
        marker = str(item["marker"])
        if marker in markers:
            return "ledger_duplicate_marker"
        markers.add(marker)
    return None


__all__ = [
    "MAX_CONSUMED_DUE",
    "MAX_UNREFRESHABLE",
    "MAX_CONFLICT_RETRY",
    "MAX_OWNER_OFFSETS",
    "SCHEDULER_SCHEMA_VERSION",
    "claim_digest",
    "consumed_entry",
    "conflict_retry_entry",
    "conflict_retry_frontier",
    "conflict_retry_generation",
    "conflict_retry_key",
    "conflict_retry_suppressed",
    "identity_marker",
    "merge_bounded_ledger",
    "merge_conflict_retry_ledger",
    "validate_bounded_ledger",
    "validate_refresh_state",
    "owner_key",
    "schedule_claim",
    "schedule_marker",
    "select_fair",
    "tier_sort_key",
    "unrefreshable_marker",
    "unrefreshable_entry",
]
