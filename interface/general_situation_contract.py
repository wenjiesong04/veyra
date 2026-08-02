from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


ANCHOR_KINDS = frozenset(
    {
        "goal",
        "commitment",
        "case",
        "task",
        "trace",
        "entity",
        "workspace",
    }
)
DURABLE_ANCHOR_KINDS = frozenset({"goal", "commitment"})
WORKSPACE_ANCHOR_KIND = "workspace"

_STRUCTURED_REF_ID = re.compile(r"^[A-Za-z0-9_./:@+%#=~-]{1,240}$")


def stable_digest(namespace: str, value: Any) -> str:
    encoded = json.dumps(
        {"namespace": namespace, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class StructuredAnchor:
    kind: str
    ref_id: str

    def __post_init__(self) -> None:
        selected_kind = str(self.kind or "").strip().lower()
        selected_ref = str(self.ref_id or "").strip()
        if selected_kind not in ANCHOR_KINDS:
            raise ValueError("unsupported structured anchor kind")
        if not _STRUCTURED_REF_ID.fullmatch(selected_ref):
            raise ValueError("structured anchor ref_id is invalid")
        object.__setattr__(self, "kind", selected_kind)
        object.__setattr__(self, "ref_id", selected_ref)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.ref_id}"

    @property
    def durable(self) -> bool:
        return self.kind in DURABLE_ANCHOR_KINDS

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref_id": self.ref_id}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StructuredAnchor":
        if not isinstance(value, dict) or set(value) != {"kind", "ref_id"}:
            raise ValueError("structured anchor projection is invalid")
        return cls(
            kind=str(value.get("kind") or ""),
            ref_id=str(value.get("ref_id") or ""),
        )


@dataclass(frozen=True, slots=True)
class ChildSituationRef:
    situation_id: str
    observation_revision: int
    source_event_id: str
    digest: str

    def __post_init__(self) -> None:
        situation_id = str(self.situation_id or "").strip()
        source_event_id = str(self.source_event_id or "").strip()
        digest = str(self.digest or "").strip().lower()
        revision = self.observation_revision
        if not situation_id or len(situation_id) > 240:
            raise ValueError("situation_id is invalid")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("observation_revision must be a positive integer")
        if not source_event_id or len(source_event_id) > 240:
            raise ValueError("source_event_id is invalid")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("child situation digest must be sha256 hex")
        object.__setattr__(self, "situation_id", situation_id)
        object.__setattr__(self, "source_event_id", source_event_id)
        object.__setattr__(self, "digest", digest)

    @property
    def key(self) -> str:
        return stable_digest(
            "veyra.general_situation.child_ref.v1",
            self.to_dict(),
        )

    def to_dict(self) -> dict[str, Any]:
        # This is intentionally the entire child projection allowed in a
        # general Situation. Decision, outcome, observations, and user text are
        # never copied into the parent.
        return {
            "situation_id": self.situation_id,
            "observation_revision": self.observation_revision,
            "source_event_id": self.source_event_id,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ChildSituationRef":
        return cls(
            situation_id=str(value.get("situation_id") or ""),
            observation_revision=value.get("observation_revision"),
            source_event_id=str(value.get("source_event_id") or ""),
            digest=str(value.get("digest") or ""),
        )
