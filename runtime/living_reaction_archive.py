"""Immutable, content-addressed segments for the Living Reaction ledger.

The hot reaction state is intentionally small.  This module owns the durable
archive seam used when old reactions, feedback, or policy revisions leave the
hot working set.  A manifest is mutable only under the WorldState writer
fence; segment files are immutable and named by their content digest.

The archive does not make any authority decision.  It only stores validated
record copies so exact replay remains possible after hot-state compaction.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from interface.living_reaction_contract import LivingReactionValidationError, parse_time, stable_digest, time_iso


class LivingReactionArchiveError(RuntimeError):
    """Archive is unavailable, corrupt, or outside its authority scope."""


class LivingReactionArchive:
    """Read/write immutable archive segments under one state-root fence."""

    MANIFEST_FILE = "living_reaction_archive.json"
    MANIFEST_SCHEMA = "veyra.living_reaction_archive.v1"
    SEGMENT_SCHEMA = "veyra.living_reaction_archive_segment.v1"
    SEGMENT_DIR = "living_reaction_archive"
    MAX_SEGMENTS = 4096

    def __init__(self, state_store: Any, *, authority: Mapping[str, bool]) -> None:
        self.state_store = state_store
        self.authority = dict(authority)

    @staticmethod
    def _empty_manifest(authority: Mapping[str, bool]) -> dict[str, Any]:
        return {
            "schema_version": LivingReactionArchive.MANIFEST_SCHEMA,
            "segments": [],
            "head_digest": "",
            "authority": dict(authority),
        }

    def read(self) -> dict[str, Any]:
        """Validate the manifest, every referenced segment, and the chain."""

        raw = self.state_store.read_json(self.MANIFEST_FILE)
        if not raw:
            return self._empty_manifest(self.authority)
        if not isinstance(raw, dict):
            raise LivingReactionArchiveError("living reaction archive manifest is not an object")
        self._validate_manifest(raw)
        entries: dict[str, dict[str, dict[str, Any]]] = {
            "reactions": {},
            "feedback": {},
            "policy_revisions": {},
        }
        previous = ""
        for item in raw["segments"]:
            segment = self._read_segment(item)
            if segment["previous_digest"] != previous:
                raise LivingReactionArchiveError("living reaction archive chain is broken")
            previous = str(segment["digest"])
            for kind in entries:
                rows = segment["entries"].get(kind, [])
                for row in rows:
                    identity = self._row_identity(kind, row)
                    existing = entries[kind].get(identity)
                    if existing is not None and existing != row:
                        raise LivingReactionArchiveError("living reaction archive has conflicting duplicate records")
                    entries[kind][identity] = copy.deepcopy(row)
        if str(raw.get("head_digest") or "") != previous:
            raise LivingReactionArchiveError("living reaction archive head is invalid")
        return {
            "manifest": copy.deepcopy(raw),
            "reactions": entries["reactions"],
            "feedback": entries["feedback"],
            "policy_revisions": entries["policy_revisions"],
        }

    def append(self, entries: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        """Append one immutable segment, then publish its manifest pointer.

        The caller invokes this from inside ``WorldStateStore.mutate_json``.
        The segment is fsynced before the hot-state mutator removes any row,
        which makes archive-first recovery safe after a process interruption.
        """

        selected = self._normalise_entries(entries)
        if not any(selected.values()):
            return self.read()["manifest"]
        current = self.read()
        manifest = current["manifest"]
        if len(manifest["segments"]) >= self.MAX_SEGMENTS:
            raise LivingReactionArchiveError("living reaction archive segment capacity exhausted")
        previous = str(manifest.get("head_digest") or "")
        created_at = time_iso(now or datetime.now(timezone.utc))
        scope_digests = sorted(self._scope_digests(selected))
        body = {
            "schema_version": self.SEGMENT_SCHEMA,
            "previous_digest": previous,
            "created_at": created_at,
            "authority": dict(self.authority),
            "scope_digests": scope_digests,
            "entries": selected,
        }
        digest = stable_digest("veyra.living_reaction.archive.segment.v1", body)
        segment = {**body, "digest": digest}
        path = self._segment_path(digest)
        existing = self._read_json_file(path)
        if existing is None:
            writer = getattr(self.state_store, "_write_json_atomic", None)
            if not callable(writer):
                raise LivingReactionArchiveError("state store lacks atomic archive writer")
            writer(path, segment)
        elif existing != segment:
            raise LivingReactionArchiveError("content-addressed archive segment conflicts with existing file")

        segment_ref = {
            "digest": digest,
            "path": str(path.relative_to(self.state_store.path_for(self.MANIFEST_FILE).parent)),
            "previous_digest": previous,
            "scope_digests": scope_digests,
            "created_at": created_at,
        }

        def update(current_manifest: dict[str, Any]) -> dict[str, Any]:
            if not current_manifest:
                current_manifest.update(self._empty_manifest(self.authority))
            self._validate_manifest(current_manifest)
            if str(current_manifest.get("head_digest") or "") != previous:
                raise LivingReactionArchiveError("living reaction archive head changed during append")
            current_manifest["segments"].append(segment_ref)
            current_manifest["head_digest"] = digest
            return current_manifest

        self.state_store.mutate_json(self.MANIFEST_FILE, update)
        return self.read()["manifest"]

    def _segment_path(self, digest: str) -> Path:
        root = self.state_store.path_for(self.MANIFEST_FILE).parent / self.SEGMENT_DIR
        return root / f"segment_{digest}.json"

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LivingReactionArchiveError("living reaction archive segment is unreadable") from exc
        if not isinstance(raw, dict):
            raise LivingReactionArchiveError("living reaction archive segment is not an object")
        return raw

    def _read_segment(self, reference: Mapping[str, Any]) -> dict[str, Any]:
        digest = str(reference.get("digest") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise LivingReactionArchiveError("living reaction archive segment digest is invalid")
        path_value = str(reference.get("path") or "")
        expected_path = f"{self.SEGMENT_DIR}/segment_{digest}.json"
        if path_value != expected_path:
            raise LivingReactionArchiveError("living reaction archive segment path is invalid")
        path = self.state_store.path_for(self.MANIFEST_FILE).parent / path_value
        segment = self._read_json_file(path)
        if segment is None:
            raise LivingReactionArchiveError("living reaction archive segment is missing")
        self._validate_segment(segment)
        if segment["digest"] != digest or segment["previous_digest"] != reference.get("previous_digest"):
            raise LivingReactionArchiveError("living reaction archive segment reference is invalid")
        if segment["scope_digests"] != reference.get("scope_digests"):
            raise LivingReactionArchiveError("living reaction archive segment scope is invalid")
        return segment

    def _validate_manifest(self, manifest: Mapping[str, Any]) -> None:
        if manifest.get("schema_version") != self.MANIFEST_SCHEMA or manifest.get("authority") != self.authority:
            raise LivingReactionArchiveError("living reaction archive manifest authority is invalid")
        segments = manifest.get("segments")
        if not isinstance(segments, list) or len(segments) > self.MAX_SEGMENTS:
            raise LivingReactionArchiveError("living reaction archive manifest segments are invalid")
        head = manifest.get("head_digest")
        if not isinstance(head, str) or len(head) not in (0, 64):
            raise LivingReactionArchiveError("living reaction archive head is invalid")
        previous = ""
        seen: set[str] = set()
        for reference in segments:
            if not isinstance(reference, Mapping):
                raise LivingReactionArchiveError("living reaction archive segment reference is invalid")
            digest = str(reference.get("digest") or "")
            if digest in seen or reference.get("previous_digest") != previous:
                raise LivingReactionArchiveError("living reaction archive manifest chain is invalid")
            if not isinstance(reference.get("scope_digests"), list) or reference["scope_digests"] != sorted(set(reference["scope_digests"])):
                raise LivingReactionArchiveError("living reaction archive manifest scope index is invalid")
            seen.add(digest)
            previous = digest
        if head != previous:
            raise LivingReactionArchiveError("living reaction archive manifest head does not reconcile")

    def _validate_segment(self, segment: Mapping[str, Any]) -> None:
        if segment.get("schema_version") != self.SEGMENT_SCHEMA or segment.get("authority") != self.authority:
            raise LivingReactionArchiveError("living reaction archive segment authority is invalid")
        digest = segment.get("digest")
        body = {key: value for key, value in segment.items() if key != "digest"}
        if digest != stable_digest("veyra.living_reaction.archive.segment.v1", body):
            raise LivingReactionArchiveError("living reaction archive segment digest mismatch")
        previous = segment.get("previous_digest")
        if not isinstance(previous, str) or len(previous) not in (0, 64):
            raise LivingReactionArchiveError("living reaction archive segment predecessor is invalid")
        if not isinstance(segment.get("created_at"), str) or not segment["created_at"].strip():
            raise LivingReactionArchiveError("living reaction archive segment timestamp is invalid")
        try:
            parse_time(segment["created_at"], field_name="archive.created_at")
        except LivingReactionValidationError as exc:
            raise LivingReactionArchiveError("living reaction archive segment timestamp is invalid") from exc
        entries = segment.get("entries")
        if not isinstance(entries, Mapping) or set(entries) != {"reactions", "feedback", "policy_revisions"}:
            raise LivingReactionArchiveError("living reaction archive entries are invalid")
        for kind in entries:
            rows = entries[kind]
            if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
                raise LivingReactionArchiveError("living reaction archive record list is invalid")
            identities = [self._row_identity(kind, row) for row in rows]
            if len(set(identities)) != len(identities):
                raise LivingReactionArchiveError("living reaction archive segment contains duplicate records")
        scopes = sorted(self._scope_digests(entries))
        if not isinstance(segment.get("scope_digests"), list) or any(not isinstance(item, str) or len(item) != 64 for item in segment["scope_digests"]):
            raise LivingReactionArchiveError("living reaction archive segment scope index is invalid")
        if segment.get("scope_digests") != scopes:
            raise LivingReactionArchiveError("living reaction archive segment scope digest mismatch")

    @staticmethod
    def _row_identity(kind: str, row: Mapping[str, Any]) -> str:
        key = {"reactions": "reaction_id", "feedback": "feedback_id", "policy_revisions": "policy_revision_id"}.get(kind)
        identity = row.get(key) if key else None
        if not isinstance(identity, str) or not identity:
            raise LivingReactionArchiveError("living reaction archive record identity is invalid")
        return identity

    @staticmethod
    def _scope_digests(entries: Mapping[str, Any]) -> set[str]:
        scopes: set[str] = set()
        for row in entries.get("reactions", []) or []:
            if isinstance(row, Mapping):
                scopes.add(stable_digest("veyra.living_reaction.archive.scope.v1", {"owner_id": row.get("owner_id"), "session_id": row.get("session_id"), "situation_id": row.get("situation_id")}))
        for row in entries.get("feedback", []) or []:
            if isinstance(row, Mapping):
                scopes.add(stable_digest("veyra.living_reaction.archive.scope.v1", {"owner_id": row.get("semantics", {}).get("owner_id"), "session_id": row.get("semantics", {}).get("session_id"), "situation_id": row.get("semantics", {}).get("situation_id")}))
        for row in entries.get("policy_revisions", []) or []:
            if isinstance(row, Mapping):
                scopes.add(stable_digest("veyra.living_reaction.archive.category_scope.v1", {"owner_id": row.get("owner_id"), "category": row.get("category")}))
        return scopes

    @staticmethod
    def _normalise_entries(entries: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
        if not isinstance(entries, Mapping) or set(entries) - {"reactions", "feedback", "policy_revisions"}:
            raise LivingReactionArchiveError("archive entry kinds are unsupported")
        result: dict[str, list[dict[str, Any]]] = {}
        for kind in ("reactions", "feedback", "policy_revisions"):
            rows = entries.get(kind, []) if isinstance(entries, Mapping) else []
            if not isinstance(rows, (list, tuple)):
                raise LivingReactionArchiveError("archive rows must be lists")
            if any(not isinstance(row, Mapping) for row in rows):
                raise LivingReactionArchiveError("archive rows must be objects")
            result[kind] = sorted((copy.deepcopy(dict(row)) for row in rows), key=lambda row: str(row.get({"reactions": "reaction_id", "feedback": "feedback_id", "policy_revisions": "policy_revision_id"}[kind]) or ""))
        return result


__all__ = ["LivingReactionArchive", "LivingReactionArchiveError"]
