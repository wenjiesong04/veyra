#!/usr/bin/env python3
"""Focused contract smoke for the first Living Context slice."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402

from interface.living_context_contract import (  # noqa: E402
    CandidateNeed,
    LivingContextCandidate,
    parse_living_context_candidate,
)


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"ok - {label}")


def main() -> int:
    base = {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": "create",
        "create_subject": "bounded real-life concern",
        "title": "A concern",
        "summary": "The user reported one concern.",
        "goal": "Understand the concern.",
        "lifecycle": "active",
        "known": [],
        "unknown": ["one missing fact"],
        "timeline": [],
        "material_change": "",
        "needs": [
            {
                "blocked_judgment": "one missing fact",
                "evidence_kind": "user",
                "why_now": "It changes the next judgment.",
                "urgency": 0.5,
                "expires_at": None,
                "allowed_source_classes": ["user"],
                "fallback_reaction": "ask",
                "question": "Can you clarify it?",
            }
        ],
        "answered_need_tokens": [],
        "requested_reaction": "ask",
        "source": "model",
    }
    candidate = LivingContextCandidate.model_validate(base, strict=True)
    expect(candidate.disposition == "create", "strict candidate validates")
    expect(candidate.needs[0].evidence_kind == "user", "need evidence kind is typed")

    invalid_extra = dict(base)
    invalid_extra["command"] = "do something"
    try:
        LivingContextCandidate.model_validate(invalid_extra, strict=True)
    except ValidationError:
        expect(True, "candidate rejects authority-like extra fields")
    else:  # pragma: no cover - smoke assertion
        expect(False, "candidate rejects authority-like extra fields")

    invalid_existing = {**base, "disposition": "update", "create_subject": ""}
    try:
        LivingContextCandidate.model_validate(invalid_existing, strict=True)
    except ValidationError:
        expect(True, "update requires server-issued Situation token")
    else:  # pragma: no cover
        expect(False, "update requires server-issued Situation token")

    parsed, issues = parse_living_context_candidate({**base, "source": "other"})
    expect(
        parsed is not None and not issues and parsed.source == "model",
        "candidate source is server-attested at the model seam",
    )

    try:
        CandidateNeed.model_validate(
            {
                "blocked_judgment": "x",
                "evidence_kind": "user",
                "why_now": "x",
                "allowed_source_classes": ["tool"],
                "fallback_reaction": "ask",
                "question": "x",
            },
            strict=True,
        )
    except ValidationError:
        # The source allowlist is enforced by InformationNeedRuntime as well;
        # this contract deliberately keeps source classes open only to the
        # enumerated product categories.
        expect(False, "candidate source category shape")
    else:
        expect(True, "candidate source category shape")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
