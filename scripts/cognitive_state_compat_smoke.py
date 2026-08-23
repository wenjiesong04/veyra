#!/usr/bin/env python3
"""Focused smoke for legacy cognitive-cycle projection compatibility."""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context_scope import tenant_scope_storage_key  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from runtime.read_only_cognitive_loop import ReadOnlyCognitiveLoopRuntime  # noqa: E402


OWNER = "legacy-cognitive-owner"
SESSION = "legacy-cognitive-session"


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def cycle(
    runtime: ReadOnlyCognitiveLoopRuntime,
    payload: dict[str, Any],
    *,
    evidence_refs: list[str],
    candidate_recorded: bool = True,
) -> dict[str, Any]:
    return {
        "schema_version": runtime.CYCLE_SCHEMA_VERSION,
        "cycle_id": "cog_0123456789abcdef",
        "user_id": OWNER,
        "session_id": SESSION,
        "reason": "compatibility_smoke",
        "status": "observed",
        "mode": "record_only",
        "world_digest": "a" * 64,
        "selected_opportunity_tokens": [],
        "view_digests": {},
        "selected_kinds": ["living_context"],
        "evidence_refs": list(evidence_refs),
        "selected_observations": [
            {
                "kind": "living_context",
                "payload": copy.deepcopy(payload),
                "evidence_refs": list(evidence_refs),
            }
        ],
        "brief": {
            "disposition": "record_candidate" if candidate_recorded else "quiet",
            "material_changes": [{"statement": "legacy history"}]
            if candidate_recorded
            else [],
        },
        "baseline": False,
        "candidate_recorded": candidate_recorded,
        "ungrounded_claim_count": 0,
        "epistemic_status": "hypothesis",
        "is_fact": False,
        "authority": False,
        "external_delivery": False,
        "agent_execution": False,
        "tool_execution": False,
        "route_change_allowed": False,
        "risk_change_allowed": False,
        "model_calls": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def install_cycle(
    store: WorldStateStore,
    runtime: ReadOnlyCognitiveLoopRuntime,
    item: dict[str, Any],
) -> None:
    scope_key = tenant_scope_storage_key(OWNER, SESSION)
    state = store.read_json(runtime.STATE_FILE)
    scopes = state["scopes"]
    scopes[scope_key] = {
        "user_id": OWNER,
        "session_id": SESSION,
        "cycles": [copy.deepcopy(item)],
        "cycle_count": 1,
        "last_brief": copy.deepcopy(item.get("brief")),
        "last_checked_at": item["created_at"],
        "last_cycle_id": item["cycle_id"],
        "last_model_at": item["created_at"],
        "last_world_digest": item["world_digest"],
        "updated_at": item["created_at"],
    }
    state["metrics"] = runtime._metrics(scopes, state.get("continuity") or {})  # noqa: SLF001
    store.write_json(runtime.STATE_FILE, state)


def main() -> int:
    with TemporaryDirectory(prefix="veyra-cognitive-state-compat-") as directory:
        store = WorldStateStore(Path(directory) / "state")
        runtime = ReadOnlyCognitiveLoopRuntime(state_store=store, reasoning=None)

        legacy_payload = {
            "scope": "exact_owner_session",
            "situations": [
                {
                    "status": "active",
                    "revision": 1,
                    "title": "历史 Situation",
                    "summary": "旧版投影只用于历史阅读。",
                }
            ],
            "reaction_count": 1,
            "reactions": [{"disposition": "wait"}],
            "feedback_count": 1,
            "feedback": [{"label": "not_now"}],
        }
        legacy_ref = runtime._projection_ref("living_context", legacy_payload)  # noqa: SLF001
        legacy = cycle(runtime, legacy_payload, evidence_refs=[legacy_ref])
        install_cycle(store, runtime, legacy)
        state = store.read_json(runtime.STATE_FILE)
        expect(runtime._valid_cognitive_state(state), "legacy cycle remains readable")  # noqa: SLF001
        candidates, rejections, error = runtime._build_v1_suggestion_candidates_detailed(legacy)  # noqa: SLF001
        expect(not candidates and error == "legacy_living_context_projection", "legacy cycle cannot enter V1 candidate bridge")
        expect(rejections == [], "legacy compatibility does not fabricate item rejections")

        bound_payload = copy.deepcopy(legacy_payload)
        bound_payload["situations"][0].update(
            {
                "change_token": "lcchg_" + "b" * 32,
                "row_evidence_ref": "lcref_" + "c" * 32,
                "row_novelty": "new",
            }
        )
        bound_ref_payload = runtime._living_context_projection_payload(bound_payload)  # noqa: SLF001
        bound_ref = runtime._projection_ref("living_context", bound_ref_payload)  # noqa: SLF001
        bound = cycle(
            runtime,
            bound_payload,
            evidence_refs=[bound_ref, bound_payload["situations"][0]["row_evidence_ref"]],
            candidate_recorded=False,
        )
        install_cycle(store, runtime, bound)
        expect(runtime._valid_cognitive_state(store.read_json(runtime.STATE_FILE)), "current row-bound cycle remains valid")  # noqa: SLF001

        mixed_payload = copy.deepcopy(bound_payload)
        mixed_payload["situations"].append(copy.deepcopy(legacy_payload["situations"][0]))
        mixed_ref = runtime._projection_ref(
            "living_context",
            runtime._living_context_projection_payload(mixed_payload),  # noqa: SLF001
        )
        mixed = cycle(runtime, mixed_payload, evidence_refs=[mixed_ref], candidate_recorded=False)
        install_cycle(store, runtime, mixed)
        invalid = store.read_json(runtime.STATE_FILE)
        expect(not runtime._valid_cognitive_state(invalid), "mixed row generations fail closed")  # noqa: SLF001
        expect(
            runtime._cognitive_state_validation_reason(invalid) == "state_cycle_record_invalid",  # noqa: SLF001
            "status diagnostic is bounded",
        )
        status = runtime.status()
        expect(status["validation_reason"] == "state_cycle_record_invalid", "status exposes bounded validation reason")
    print("PASS cognitive state compatibility")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
