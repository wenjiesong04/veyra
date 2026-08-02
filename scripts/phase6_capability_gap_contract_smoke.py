#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from typing import Any, Callable

from pydantic import TypeAdapter, ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.capability_gap import (  # noqa: E402
    CapabilityGapAuthority,
    CapabilityGapLifecycleReceipt,
    deterministic_capability_gap_id,
)
from scripts.phase6_capability_gap_test_support import (  # noqa: E402
    CONTROL_TOKEN,
    RAW_TEXT,
    SESSION,
    USER,
    WORKSPACE,
    generation_receipt,
    prepare_context,
    proposal_payload,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def rejected(
    payload: dict[str, Any], mutate: Callable[[dict[str, Any]], None]
) -> bool:
    selected = copy.deepcopy(payload)
    mutate(selected)
    try:
        TypeAdapter(CapabilityGapLifecycleReceipt).validate_python(
            selected, strict=True
        )
    except ValidationError:
        return True
    return False


def main() -> int:
    context = prepare_context()
    try:
        receipt = generation_receipt(context)
        parsed = TypeAdapter(CapabilityGapLifecycleReceipt).validate_python(
            receipt, strict=True
        )
        expect(
            parsed.receipt_kind == "generation"
            and parsed.authority_granted is False
            and parsed.digest() == parsed.digest(),
            "source-free generation receipt has a stable strict identity",
        )
        cases: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
            (
                "unknown receipt fields are rejected",
                lambda value: value.__setitem__("source", RAW_TEXT),
            ),
            (
                "authority cannot be granted by a receipt",
                lambda value: value.__setitem__("authority_granted", True),
            ),
            (
                "candidate revision coercion is rejected",
                lambda value: value.__setitem__("candidate_revision", "2"),
            ),
            (
                "candidate identity changes are rejected",
                lambda value: value.__setitem__("candidate_id", "extspec_bad"),
            ),
            (
                "noncanonical receipt source is rejected",
                lambda value: value.__setitem__("receipt_source", "model"),
            ),
            (
                "unbounded generation status is rejected",
                lambda value: value.__setitem__(
                    "generation_status", "looks_good"
                ),
            ),
        ]
        for label, mutate in cases:
            expect(rejected(receipt, mutate), label)

        proposal = proposal_payload()
        inputs = {
            "proposal_id": proposal["proposal_id"],
            "intent_id": "pin_capabilitygap01",
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "session_id": SESSION,
            "source_code": proposal["source"],
        }
        first = deterministic_capability_gap_id(**inputs)
        second = deterministic_capability_gap_id(**dict(reversed(list(inputs.items()))))
        expect(
            first == second and first.startswith("capgap_") and len(first) == 31,
            "capability-gap identifier is deterministic and content bound",
            first,
        )

        authority = CapabilityGapAuthority().model_dump(mode="json")
        expect(
            authority["capability_gap_recording"] is True
            and authority["lifecycle_observation"] is True
            and all(
                authority[field] is False
                for field in (
                    "schema_inference",
                    "code_generation",
                    "model_call",
                    "agent_dispatch",
                    "tool_call",
                    "execution",
                    "signing",
                    "canary",
                    "promotion",
                    "background_advancement",
                )
            ),
            "gap contract grants recording but no extension authority",
            authority,
        )
        encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
        expect(
            RAW_TEXT not in encoded
            and "raw_text" not in encoded
            and "topic" not in encoded
            and "purpose" not in encoded,
            "receipt contract has no raw natural-language field",
        )

        try:
            context.registry.status(
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                control_token="wrong-control-token",
            )
        except Exception:
            pass
        else:
            raise AssertionError("invalid control token was accepted")
        expect(True, "private registry rejects a different control principal")
        expect(
            CONTROL_TOKEN not in json.dumps(authority),
            "control token is absent from public authority projection",
        )
    finally:
        context.close()
    print("Phase 6 capability-gap contract smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
