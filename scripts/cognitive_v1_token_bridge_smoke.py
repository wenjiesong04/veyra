#!/usr/bin/env python3
"""Focused smoke for the V1 cognitive opaque Situation-token bridge."""

from __future__ import annotations

import copy
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from core.context_scope import tenant_scope_storage_key  # noqa: E402
from core.model_client import redact_sensitive  # noqa: E402
from interface.cognitive_brief_contract import (  # noqa: E402
    CognitiveBrief,
    COGNITIVE_BRIEF_MAX_LIVING_SITUATION_ROWS,
    COGNITIVE_BRIEF_MAX_UNKNOWN,
)
from interface.general_situation_contract import stable_digest  # noqa: E402
from runtime.read_only_cognitive_loop import COGNITIVE_BRIEF_SYSTEM, ReadOnlyCognitiveLoopRuntime  # noqa: E402


OWNER = "owner-cognitive-v1"
SESSION = "session-cognitive-v1"


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def situation(situation_id: str, title: str, revision: int = 1) -> dict[str, object]:
    return {
        "record_kind": "semantic_situation",
        "situation_id": situation_id,
        "user_id": OWNER,
        "session_id": SESSION,
        "status": "active",
        "observation_revision": revision,
        "semantic": {
            "title": title,
            "summary": f"当前正在处理：{title}",
            "goal": "保持事情可推进",
            "category": "work",
            "lifecycle": "active",
            "known": [],
            "unknown": [],
            "assumptions": [],
        },
        "updated_at": f"2026-08-23T00:00:0{revision}Z",
    }


def documents(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        "living_situation": {"situations": rows},
        "information_needs": {"needs": {}},
        "living_source": {"requests": {}, "receipts": {}},
        "living_reaction": {"reactions": {}, "feedback": {}},
    }


def make_cycle(
    runtime: ReadOnlyCognitiveLoopRuntime,
    view: dict[str, object],
    *,
    token: str,
    evidence_refs: list[str],
    cycle_id: str = "cog_0123456789abcdef",
) -> dict[str, object]:
    selected_view = {
        key: copy.deepcopy(value)
        for key, value in view.items()
        if key not in {"_server_situation_row_digests", "_server_living_context_handles"}
    }
    brief = CognitiveBrief.model_validate(
        {
            "schema_version": "veyra.cognitive_brief.v1",
            "disposition": "record_candidate",
            "summary_if_asked": "检测到一个需要继续跟进的变化。",
            "known": [],
            "unknown": [],
            "assumptions": [],
            "material_changes": [
                {
                    "kind": "living_context_change",
                    "subject": "当前 Situation",
                    "statement": "事情需要继续跟进",
                    # The model change cites only its selected row; the
                    # selected view itself still exposes every row ref.
                    "evidence_refs": evidence_refs[:2],
                    "why_now": "当前来源状态发生变化",
                    "confidence": 0.8,
                    "change_token": token,
                    "suggested_next_step": "确认下一步",
                }
            ],
            "why_now": "当前来源状态发生变化",
            "confidence": 0.8,
            "source": "model",
        },
        strict=True,
    )
    return {
        "schema_version": runtime.CYCLE_SCHEMA_VERSION,
        "candidate_recorded": True,
        "status": "observed",
        "mode": "record_only",
        "reason": "token_bridge_smoke",
        "user_id": OWNER,
        "session_id": SESSION,
        "cycle_id": cycle_id,
        "world_digest": "a" * 64,
        "selected_opportunity_tokens": [],
        "selected_kinds": ["living_context", "runtime_health"],
        "view_digests": {},
        "situation_row_digests": {},
        "evidence_refs": sorted(set(evidence_refs)),
        "baseline": False,
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
        "brief": brief.model_dump(mode="json"),
        "selected_observations": [
            {
                "kind": "living_context",
                "payload": selected_view,
                "evidence_refs": evidence_refs[:-1],
            },
            {
                "kind": "runtime_health",
                "payload": {"status": "ok"},
                "evidence_refs": evidence_refs[-1:],
            },
        ],
    }


def main() -> None:
    expect("dominant language" in COGNITIVE_BRIEF_SYSTEM, "brief contract preserves Situation language")
    expect("concrete, reversible" in COGNITIVE_BRIEF_SYSTEM, "record candidate requires reversible grounded next step")
    expect("Newly available first evidence" in COGNITIVE_BRIEF_SYSTEM, "first decision-relevant evidence may form a candidate without being called a change")
    with tempfile.TemporaryDirectory(prefix="veyra-cognitive-v1-") as root:
        store = WorldStateStore(root)
        rows = [situation("sit-one", "活动安排"), situation("sit-two", "面试准备")]
        rows[0]["semantic"]["known"] = [
            {
                "statement": "最新的服务器 Known",
                "epistemic_status": "reported",
                "recorded_at": "2026-08-23T09:05:00+00:00",
            },
            {
                "statement": "较早的服务器 Known",
                "epistemic_status": "reported",
                "recorded_at": "2026-08-23T08:48:00+00:00",
            },
        ]
        store.write_json("situation_state.json", {"situations": rows})
        runtime = ReadOnlyCognitiveLoopRuntime(state_store=store, reasoning=None)
        view = runtime._living_context_payload(  # noqa: SLF001
            documents(rows), user_id=OWNER, session_id=SESSION
        )
        tokens = [str(row["change_token"]) for row in view["situations"]]
        expect(len(tokens) == 2 and len(set(tokens)) == 2, "Situation tokens must be unique")
        expect(all("situation_id" not in row for row in view["situations"]), "raw ids must stay server-side")
        first_projection = view["situations"][0]
        expect(first_projection["known_order"] == "newest_first", "Known ordering is server-declared")
        expect(
            first_projection["known_current"]["statement"] == "最新的服务器 Known",
            "Known current is the newest structured server record",
        )
        # Receipt/timeline/evidence changes remain exact-token-bound, but are
        # audit-only and therefore must not make the cognitive row novel.
        audit_rows = copy.deepcopy(rows)
        audit_rows[0]["observation_revision"] = 2
        audit_rows[0]["semantic"].update(
            {
                "timeline": [{"event": "receipt-recorded"}],
                "evidence": [{"receipt_id": "receipt-audit-only"}],
                "source_observation_digests": {"a" * 64: "b" * 64},
            }
        )
        audit_view = runtime._living_context_payload(  # noqa: SLF001
            documents(audit_rows),
            user_id=OWNER,
            session_id=SESSION,
            previous_row_digests=view["_server_situation_row_digests"],
        )
        audit_first = next(row for row in audit_view["situations"] if row["title"] == "活动安排")
        expect(audit_first["row_novelty"] == "unchanged", "audit-only semantic churn leaves row novelty unchanged")
        expect(audit_first["change_token"] != first_projection["change_token"], "audit-only churn rotates exact binding token")
        store.write_json("situation_state.json", {"situations": audit_rows})
        audit_cycle = make_cycle(
            runtime,
            audit_view,
            token=str(audit_first["change_token"]),
            evidence_refs=[
                "view:audit-only",
                str(audit_first["row_evidence_ref"]),
                "view:runtime-health",
            ],
        )
        audit_brief = CognitiveBrief.model_validate(audit_cycle["brief"], strict=True)
        audit_rejected = runtime._validate_v1_material_changes(  # noqa: SLF001
            audit_brief,
            selected=audit_cycle["selected_observations"],
            allowed_refs=set(audit_cycle["evidence_refs"]),
        )
        expect(
            audit_rejected and audit_rejected[0]["reason"] == "living_context_change_row_not_novel",
            "current audit-bound token reaches handler validation but cannot claim material novelty",
        )
        store.write_json("situation_state.json", {"situations": rows})
        stale_previous = {
            "summary_if_asked": "旧 brief 的事实性摘要，不应锚定当前变化",
            "known": [{"statement": "旧 brief 的旧事实", "evidence_refs": ["view:old"]}],
        }
        expect(
            runtime._previous_brief_for_selected(stale_previous, [  # noqa: SLF001
                {"kind": "living_context", "payload": view}
            ])
            is None,
            "fresh Living Context rows suppress stale previous brief",
        )

        # The model page is intentionally four rows, but the server baseline
        # covers the complete bounded exact scope. Reordering five unchanged
        # rows must not make the page novel; changing the hidden fifth row must
        # still change the stable wake-up digest.
        five_rows = [situation(f"sit-five-{index}", f"事项 {index}") for index in range(5)]
        for index, row in enumerate(five_rows):
            row["updated_at"] = f"2026-08-23T00:00:{index:02d}Z"
        five_view = runtime._living_context_payload(  # noqa: SLF001
            documents(five_rows), user_id=OWNER, session_id=SESSION
        )
        five_digests = five_view["_server_situation_row_digests"]
        expect(len(five_digests) == 5, "all bounded Situation row digests are retained")
        prompt_five = {
            key: value
            for key, value in five_view.items()
            if key
            not in {"_server_situation_row_digests", "_server_living_context_handles"}
        }
        reordered_view = runtime._living_context_payload(  # noqa: SLF001
            documents(list(reversed(five_rows))),
            user_id=OWNER,
            session_id=SESSION,
            previous_row_digests=five_digests,
        )
        reordered_prompt = {
            key: value
            for key, value in reordered_view.items()
            if key
            not in {"_server_situation_row_digests", "_server_living_context_handles"}
        }
        expect(
            reordered_view["_server_situation_row_digests"] == five_digests,
            "row digest baseline is order independent",
        )
        expect(
            all(row["row_novelty"] == "unchanged" for row in reordered_view["situations"]),
            "unchanged rows are not reclassified after reorder",
        )
        stable_before = runtime._living_context_digest_payload(  # noqa: SLF001
            prompt_five,
            five_digests,
        )
        stable_reordered = runtime._living_context_digest_payload(  # noqa: SLF001
            reordered_prompt,
            reordered_view["_server_situation_row_digests"],
        )
        expect(stable_before == stable_reordered, "reorder does not change stable view digest")
        changed_five = copy.deepcopy(five_rows)
        changed_five[0]["observation_revision"] = 2
        changed_five[0]["semantic"]["summary"] = "事项 0 已有真实变化"
        changed_view = runtime._living_context_payload(  # noqa: SLF001
            documents(changed_five),
            user_id=OWNER,
            session_id=SESSION,
            previous_row_digests=five_digests,
        )
        changed_prompt = {
            key: value
            for key, value in changed_view.items()
            if key
            not in {"_server_situation_row_digests", "_server_living_context_handles"}
        }
        stable_changed = runtime._living_context_digest_payload(  # noqa: SLF001
            changed_prompt,
            changed_view["_server_situation_row_digests"],
        )
        expect(
            stable_changed != stable_before,
            "real revision or semantic change wakes the stable view",
        )

        four_rows = [
            situation("sit-one", "活动安排"),
            situation("sit-two", "面试准备"),
            situation("sit-three", "旅行计划"),
            situation("sit-four", "健康安排"),
        ]
        four_rows[0]["semantic"]["known"] = [
            {
                "statement": f"known-{index}",
                "epistemic_status": "inferred",
                "recorded_at": f"2026-08-{index + 1:02d}T00:00:00+00:00",
            }
            for index in range(6)
        ]
        store.write_json("situation_state.json", {"situations": four_rows})
        opportunities = runtime._opportunities(  # noqa: SLF001
            cycle_id="cog_fedcba9876543210",
            user_id=OWNER,
            session_id=SESSION,
        )
        model_view = next(item for item in opportunities if item["kind"] == "living_context")["payload"]
        model_rows = model_view["situations"]
        model_tokens = [row.get("change_token") for row in model_rows]
        expect(
            len(model_rows) == COGNITIVE_BRIEF_MAX_LIVING_SITUATION_ROWS
            and len(set(model_tokens)) == COGNITIVE_BRIEF_MAX_LIVING_SITUATION_ROWS,
            "bounded unique opaque handles reach the model view",
        )
        expect(all(str(token).startswith("lcchg_") for token in model_tokens), "server handles survive sensitive-field redaction")
        known_projection = next(row["known"] for row in model_rows if row.get("title") == "活动安排")
        expect(
            [row.get("statement") for row in known_projection] == ["known-5", "known-4", "known-3", "known-2"],
            "model view keeps the four freshest structured Known records",
        )
        server_view = runtime._living_context_payload(  # noqa: SLF001
            documents(four_rows),
            user_id=OWNER,
            session_id=SESSION,
        )
        handles = server_view.pop("_server_living_context_handles", [])
        forged_view = copy.deepcopy(server_view)
        forged_view["situations"][0]["change_token"] = "lcchg_" + "f" * 32
        forged_view["situations"][1]["row_evidence_ref"] = "lcref_" + "e" * 32
        restored_forged = runtime._restore_living_context_handles(  # noqa: SLF001
            original_payload=forged_view,
            redacted_payload=redact_sensitive(
                forged_view,
                max_string=300,
                max_list=COGNITIVE_BRIEF_MAX_UNKNOWN,
            ),
            handles=handles,
            owner_id=OWNER,
            session_id=SESSION,
        )
        expect(restored_forged["situations"][0]["change_token"] == "<redacted>", "foreign handle is not restored")
        expect(restored_forged["situations"][1]["row_evidence_ref"] == "<redacted>", "invalid row evidence handle is not restored")
        store.write_json("situation_state.json", {"situations": rows})

        ref_payload = {
            key: value
            for key, value in view.items()
            if key
            not in {
                "reaction_count",
                "reactions",
                "feedback_count",
                "feedback",
                "_server_situation_row_digests",
                "_server_living_context_handles",
            }
        }
        living_ref = runtime._projection_ref("living_context", ref_payload)  # noqa: SLF001
        second_ref = runtime._projection_ref("runtime_health", {"status": "ok"})  # noqa: SLF001
        row_ref = str(view["situations"][0]["row_evidence_ref"])
        second_row_ref = str(view["situations"][1]["row_evidence_ref"])
        cycle = make_cycle(
            runtime,
            view,
            token=tokens[0],
            evidence_refs=[living_ref, row_ref, second_row_ref, second_ref],
        )
        candidates, error = runtime._build_v1_suggestion_candidates(cycle)  # noqa: SLF001
        expect(error is None and len(candidates) == 1, "valid multi-ref candidate must bind")
        candidate = candidates[0]
        expect(candidate["situation_id"] == "sit-one", "server must resolve the raw id")
        expect(candidate["epistemic_status"] == "hypothesis", "candidate is not a fact")
        expect(candidate["is_fact"] is False and candidate["authority"] is False, "candidate has no authority")
        expect(row_ref in candidate["evidence_refs"], "candidate carries the exact row evidence ref")

        # Legacy observed cycles remain readable as consumed history.  A newer
        # durable rejected bridge is different: even if an older runtime had
        # copied its digest to the scope row, the effective checkpoint must
        # not treat that rejected cycle as ACKed.
        legacy_cycle = copy.deepcopy(cycle)
        legacy_cycle.pop("v1_suggestion_bridge", None)
        legacy_cycle.pop("novelty_ack", None)
        legacy_scope = {
            "last_world_digest": cycle["world_digest"],
            "last_view_digests": {},
            "last_situation_row_digests": {},
            "last_brief": cycle["brief"],
            "cycles": [legacy_cycle],
        }
        expect(
            runtime._effective_novelty_checkpoint(legacy_scope)["world_digest"]
            == cycle["world_digest"],
            "legacy cycle remains compatible",
        )
        rejected_cycle = copy.deepcopy(cycle)
        rejected_cycle["v1_suggestion_bridge"] = {
            **runtime._new_v1_suggestion_bridge(  # noqa: SLF001
                candidates=[copy.deepcopy(candidate)],
                rejections=[],
                structure_error=None,
                created_at=cycle["created_at"],
            ),
            "status": "rejected",
            "terminal": True,
        }
        rejected_scope = {**legacy_scope, "cycles": [rejected_cycle]}
        expect(
            runtime._effective_novelty_checkpoint(rejected_scope)["world_digest"] == "",
            "rejected bridge cannot masquerade as an ACK",
        )

        # The model may cite the selected Living Context page without
        # repeating the server-issued row handle.  After the opaque token and
        # current Situation binding are checked, the bridge canonicalises the
        # row ref into the candidate evidence set.
        page_only = copy.deepcopy(cycle)
        page_only["cycle_id"] = "cog_page_only_0123456"
        page_only["brief"]["material_changes"][0]["evidence_refs"] = [living_ref]
        page_only_candidates, page_only_error = runtime._build_v1_suggestion_candidates(page_only)  # noqa: SLF001
        expect(page_only_error is None and len(page_only_candidates) == 1, "page-only cite remains a valid candidate")
        expect(
            page_only_candidates[0]["evidence_refs"] == [living_ref, row_ref],
            "server adds the resolved row evidence ref to page-only candidate",
        )
        page_only_brief = CognitiveBrief.model_validate(page_only["brief"], strict=True)
        expect(
            not runtime._validate_v1_material_changes(  # noqa: SLF001
                page_only_brief,
                selected=page_only["selected_observations"],
                allowed_refs={living_ref, row_ref, second_row_ref, second_ref},
            ),
            "page-only cite passes typed-handle preflight",
        )

        replay = copy.deepcopy(cycle)
        replay["cycle_id"] = "cog_fedcba9876543210"
        replay_candidates, replay_error = runtime._build_v1_suggestion_candidates(replay)  # noqa: SLF001
        expect(replay_error is None, "replay candidate must remain valid")
        expect(replay_candidates[0]["candidate_id"] == candidate["candidate_id"], "replay id must be stable")
        expect(replay_candidates[0]["cycle_id"] != candidate["cycle_id"], "replay keeps its cycle id")

        stale = copy.deepcopy(rows)
        stale[0]["observation_revision"] = 2
        store.write_json("situation_state.json", {"situations": stale})
        stale_candidates, stale_error = runtime._build_v1_suggestion_candidates(cycle)  # noqa: SLF001
        expect(not stale_candidates and stale_error == "living_context_situation_revision_stale", "stale revision must fail closed")
        store.write_json("situation_state.json", {"situations": rows})

        foreign_token = runtime._living_change_token(  # noqa: SLF001
            owner_id="other-owner",
            session_id=SESSION,
            situation_id="sit-one",
            observation_revision=1,
            semantic_digest=runtime._living_semantic_digest(rows[0]["semantic"]),  # noqa: SLF001
        )
        for bad_token in ("lcchg_foreign", foreign_token):
            bad_cycle = make_cycle(runtime, view, token=bad_token, evidence_refs=[living_ref, row_ref])
            bad_brief = CognitiveBrief.model_validate(bad_cycle["brief"], strict=True)
            rejected = runtime._validate_v1_material_changes(  # noqa: SLF001
                bad_brief,
                selected=bad_cycle["selected_observations"],
                allowed_refs={living_ref, row_ref},
            )
            expect(
                rejected and rejected[0]["reason"] == "living_context_change_token_not_selected",
                "foreign or invented token must be rejected",
            )

        mixed_validation = copy.deepcopy(cycle)
        mixed_validation["brief"]["material_changes"].append(
            {
                **copy.deepcopy(mixed_validation["brief"]["material_changes"][0]),
                "change_token": "lcchg_not_a_server_handle",
                "statement": "伪造 sibling 不应吞掉有效 sibling",
                "evidence_refs": [living_ref],
            }
        )
        mixed_brief = CognitiveBrief.model_validate(mixed_validation["brief"], strict=True)
        mixed_rejected = runtime._validate_v1_material_changes(  # noqa: SLF001
            mixed_brief,
            selected=mixed_validation["selected_observations"],
            allowed_refs={living_ref, row_ref, second_row_ref, second_ref},
        )
        expect(
            len(mixed_rejected) == 1 and mixed_rejected[0]["item_index"] == 1,
            "invalid sibling is isolated during preflight",
        )
        mixed_candidates, mixed_item_rejections, mixed_error = runtime._build_v1_suggestion_candidates_detailed(mixed_validation)  # noqa: SLF001
        expect(
            mixed_error is None and len(mixed_candidates) == 1 and len(mixed_item_rejections) == 1,
            "valid sibling survives invalid token sibling",
        )

        default_bridge = runtime._run_v1_suggestion_bridge(cycle)  # noqa: SLF001
        expect(default_bridge["status"] == "pending", "default sink remains pending")
        expect(default_bridge["reason"] == "v1_suggestion_handler_not_configured", "missing sink is diagnosable")
        seen: list[dict[str, object]] = []
        def record(item: dict[str, Any]) -> dict[str, Any]:
            seen.append(item)
            return {"status": "recorded", "candidate_id": item["candidate_id"]}

        runtime.set_v1_suggestion_handler(record)
        handled = runtime._run_v1_suggestion_bridge(cycle)  # noqa: SLF001
        expect(handled["status"] == "handled" and handled["handled_count"] == 1, "recorded result counts as handled")
        expect(len(seen) == 1, "handler receives the candidate once")
        suppressed = runtime._normalize_v1_handler_result(  # noqa: SLF001
            {"status": "suppressed", "candidate_id": candidate["candidate_id"]},
            candidate=candidate,
        )
        expect(
            suppressed["status"] == "suppressed"
            and suppressed["terminal"] is True
            and suppressed["retryable"] is False,
            "suppressed handler results are strict terminal outcomes",
        )
        runtime.set_v1_suggestion_handler(lambda item: {"status": "rejected", "candidate_id": item["candidate_id"], "reason": "low_confidence"})
        degraded = runtime._run_v1_suggestion_bridge(cycle)  # noqa: SLF001
        expect(degraded["status"] == "rejected" and degraded["handled_count"] == 0, "rejected result is not counted as handled")

        low_confidence = copy.deepcopy(cycle)
        low_confidence["brief"]["material_changes"][0]["confidence"] = 0.4
        low_candidates, low_error = runtime._build_v1_suggestion_candidates(low_confidence)  # noqa: SLF001
        expect(not low_candidates and low_error == "cognitive_suggestion_confidence_below_threshold", "low confidence is an item rejection")

        # Stable identity includes every semantically visible field, while a
        # cycle replay alone does not change the candidate id.
        for field, value in {
            "change_token": tokens[1],
            "statement": "另一条建议",
            "why_now": "新的原因",
            "suggested_next_step": "另一下一步",
            "evidence_refs": [living_ref, second_row_ref],
            "confidence": 0.7,
        }.items():
            changed = copy.deepcopy(cycle)
            changed["cycle_id"] = "cog_fedcba9876543210"
            changed["brief"]["material_changes"][0][field] = value
            if field == "change_token":
                changed["brief"]["material_changes"][0]["evidence_refs"] = [living_ref, second_row_ref]
            elif field == "evidence_refs":
                changed["brief"]["material_changes"][0]["evidence_refs"] = [living_ref, row_ref, second_ref]
            changed_candidates, changed_error = runtime._build_v1_suggestion_candidates(changed)  # noqa: SLF001
            expect(changed_error is None and changed_candidates[0]["candidate_id"] != candidate["candidate_id"], f"candidate id includes {field}")

        # Valid and stale siblings are resolved independently.
        mixed = copy.deepcopy(cycle)
        mixed["brief"]["material_changes"].append({
            **copy.deepcopy(mixed["brief"]["material_changes"][0]),
            "change_token": tokens[1],
            "statement": "第二个 Situation 也有变化",
            "evidence_refs": [living_ref, second_row_ref, second_ref],
        })
        changed_rows = copy.deepcopy(rows)
        changed_rows[1]["observation_revision"] = 2
        store.write_json("situation_state.json", {"situations": changed_rows})
        mixed_candidates, mixed_rejections, mixed_error = runtime._build_v1_suggestion_candidates_detailed(mixed)  # noqa: SLF001
        expect(mixed_error is None and len(mixed_candidates) == 1, "valid sibling survives stale sibling")
        expect(len(mixed_rejections) == 1 and mixed_rejections[0]["status"] == "stale", "stale sibling is diagnosed")
        store.write_json("situation_state.json", {"situations": rows})

        # The first durable cycle carries the complete candidate and a
        # pending bridge.  Reconciliation retries it independently of a
        # world-digest comparison and then CAS-closes the exact cycle.
        cycle["v1_suggestion_bridge"] = runtime._new_v1_suggestion_bridge(  # noqa: SLF001
            candidates=[copy.deepcopy(candidate)], rejections=[], structure_error=None, created_at=cycle["created_at"]
        )
        config = runtime._config()  # noqa: SLF001
        persisted = runtime._persist_cycle(  # noqa: SLF001
            scope_key=tenant_scope_storage_key(OWNER, SESSION),
            cycle=cycle,
            expected_config=config,
            expected_generation=0,
        )
        expect(persisted, "pending candidate cycle is durable before handoff")
        pending_scope = next(iter(store.read_json("cognitive_loop_state.json")["scopes"].values()))
        expect(
            pending_scope.get("last_world_digest") != cycle["world_digest"],
            "pending bridge does not advance the novelty checkpoint",
        )
        pending_state = copy.deepcopy(store.read_json("cognitive_loop_state.json"))
        crash_store = WorldStateStore(tempfile.mkdtemp(prefix="veyra-cognitive-crash-"))
        crash_store.write_json("situation_state.json", {"situations": rows})
        pending_state.pop("_state_revision", None)
        crash_store.write_json("cognitive_loop_state.json", pending_state)
        crash_runtime = ReadOnlyCognitiveLoopRuntime(state_store=crash_store, reasoning=None)
        crash_seen: list[str] = []
        crash_runtime._persist_v1_suggestion_result = lambda **_: (_ for _ in ()).throw(RuntimeError("crash after handler"))  # type: ignore[method-assign]  # noqa: SLF001
        try:
            crash_runtime.set_v1_suggestion_handler(
                lambda item: crash_seen.append(str(item["candidate_id"]))
                or {"status": "recorded", "candidate_id": item["candidate_id"]}
            )
        except RuntimeError:
            pass
        expect(crash_seen == [candidate["candidate_id"]], "handler ran before simulated CAS crash")
        crash_runtime._persist_v1_suggestion_result = ReadOnlyCognitiveLoopRuntime._persist_v1_suggestion_result.__get__(crash_runtime)  # type: ignore[method-assign]  # noqa: SLF001
        recovered_after_crash = crash_runtime._run_v1_suggestion_bridge(
            cycle,
            expected_config=crash_runtime._config(),  # noqa: SLF001
            expected_generation=0,
        )
        expect(recovered_after_crash["status"] == "handled", "CAS crash leaves a replayable pending bridge")
        expect(crash_seen == [candidate["candidate_id"], candidate["candidate_id"]], "crash replay uses stable candidate id")
        crash_runtime._reconcile_v1_suggestion_bridges(  # noqa: SLF001
            expected_config=crash_runtime._config(),
            expected_generation=0,
        )
        crash_cycle_after_ack = next(
            iter(crash_store.read_json("cognitive_loop_state.json")["scopes"].values())
        )["cycles"][0]
        expect(
            crash_cycle_after_ack["novelty_ack"]["status"] == "acked",
            "restart reconciliation ACKs a durable recorded bridge",
        )
        expect(
            next(iter(crash_store.read_json("cognitive_loop_state.json")["scopes"].values()))[
                "last_world_digest"
            ] == cycle["world_digest"],
            "recorded bridge advances the novelty checkpoint",
        )
        replay_seen: list[str] = []
        runtime.set_v1_suggestion_handler(
            lambda item: replay_seen.append(str(item["candidate_id"]))
            or {"status": "recorded", "candidate_id": item["candidate_id"]}
        )
        stored = store.read_json("cognitive_loop_state.json")
        stored_cycle = next(iter(stored["scopes"].values()))["cycles"][0]
        expect(stored_cycle["v1_suggestion_bridge"]["status"] == "handled", "replay CAS closes the pending bridge")
        expect(stored_cycle["v1_suggestion_bridge"]["status_counts"]["recorded"] == 1, "recorded result is durable")
        expect(stored_cycle["novelty_ack"]["status"] == "acked", "recorded result ACK is durable")
        runtime._reconcile_v1_suggestion_bridges(  # noqa: SLF001
            expected_config=config,
            expected_generation=0,
        )
        expect(len(replay_seen) == 1, "terminal bridge is not handed off twice")

        # A stop fence prevents a sink call and keeps a pending bridge open.
        stop_runtime = ReadOnlyCognitiveLoopRuntime(state_store=WorldStateStore(tempfile.mkdtemp(prefix="veyra-cognitive-stop-")), reasoning=None)
        stop_seen: list[str] = []
        stop_runtime.set_v1_suggestion_handler(lambda item: stop_seen.append(str(item["candidate_id"])) or {"status": "recorded", "candidate_id": item["candidate_id"]})
        stop_runtime.stop()
        fenced = stop_runtime._run_v1_suggestion_bridge(cycle)  # noqa: SLF001
        expect(fenced["status"] == "pending" and not stop_seen, "stopped bridge does not call sink")

        # A blocking local handler and stop must linearize together. stop
        # waits for the handler and its CAS; once it returns, the worker cannot
        # append a late handler result.
        block_store = WorldStateStore(tempfile.mkdtemp(prefix="veyra-cognitive-handler-stop-"))
        block_store.write_json("situation_state.json", {"situations": rows})
        block_runtime = ReadOnlyCognitiveLoopRuntime(state_store=block_store, reasoning=None)
        block_cycle = copy.deepcopy(cycle)
        block_cycle["cycle_id"] = "cog_abcdef0123456789"
        block_cycle["v1_suggestion_bridge"] = block_runtime._new_v1_suggestion_bridge(  # noqa: SLF001
            candidates=[copy.deepcopy(candidate)],
            rejections=[],
            structure_error=None,
            created_at=block_cycle["created_at"],
        )
        block_config = block_runtime._config()  # noqa: SLF001
        expect(
            block_runtime._persist_cycle(  # noqa: SLF001
                scope_key=tenant_scope_storage_key(OWNER, SESSION),
                cycle=block_cycle,
                expected_config=block_config,
                expected_generation=0,
            ),
            "blocking handler cycle is durable",
        )
        handler_entered = threading.Event()
        handler_release = threading.Event()
        handler_seen: list[str] = []

        def blocking_handler(item: dict[str, Any]) -> dict[str, Any]:
            handler_seen.append(str(item["candidate_id"]))
            handler_entered.set()
            expect(handler_release.wait(timeout=2.0), "blocking handler released")
            return {"status": "suppressed", "candidate_id": item["candidate_id"], "reason": "quiet_hours"}

        with block_runtime._worker_lock:  # noqa: SLF001
            block_runtime._v1_suggestion_handler = blocking_handler  # noqa: SLF001
        bridge_result: dict[str, Any] = {}
        bridge_thread = threading.Thread(
            target=lambda: bridge_result.update(
                block_runtime._run_v1_suggestion_bridge(  # noqa: SLF001
                    block_cycle,
                    expected_config=block_config,
                    expected_generation=0,
                )
            )
        )
        bridge_thread.start()
        expect(handler_entered.wait(timeout=1.0), "blocking handler entered")
        stop_result: dict[str, Any] = {}
        stop_done = threading.Event()

        def stop_blocked_runtime() -> None:
            stop_result.update(block_runtime.stop())
            stop_done.set()

        stop_thread = threading.Thread(target=stop_blocked_runtime)
        stop_thread.start()
        expect(not stop_done.wait(timeout=0.05), "stop waits for in-flight handler")
        handler_release.set()
        bridge_thread.join(timeout=2.0)
        stop_thread.join(timeout=2.0)
        expect(not bridge_thread.is_alive() and not stop_thread.is_alive(), "handler and stop complete")
        expect(stop_result.get("status") == "stopped", "stop returns after handler fence")
        expect(bridge_result.get("status") == "suppressed", "suppressed result is terminal")
        expect(handler_seen == [candidate["candidate_id"]], "blocking handler runs once")
        block_state_after_stop = block_store.read_json("cognitive_loop_state.json")
        block_cycle_after_stop = next(
            item
            for item in next(iter(block_state_after_stop["scopes"].values()))["cycles"]
            if item["cycle_id"] == block_cycle["cycle_id"]
        )
        expect(
            block_cycle_after_stop["v1_suggestion_bridge"]["status"] == "suppressed",
            "suppressed handler result is durably terminal",
        )
        state_snapshot = copy.deepcopy(block_state_after_stop)
        handler_release.set()
        expect(
            block_store.read_json("cognitive_loop_state.json") == state_snapshot,
            "stop has no late side effect",
        )
        block_runtime.resume()
        block_runtime._reconcile_v1_suggestion_bridges(  # noqa: SLF001
            expected_config=block_runtime._config(),
            expected_generation=2,
        )
        block_cycle_after_ack = next(
            item
            for item in next(iter(block_store.read_json("cognitive_loop_state.json")["scopes"].values()))["cycles"]
            if item["cycle_id"] == block_cycle["cycle_id"]
        )
        expect(
            block_cycle_after_ack["novelty_ack"]["reason"] == "v1_suppressed",
            "intentional suppression ACKs the novelty checkpoint",
        )

    print("Cognitive V1 token bridge smoke passed: token scope, stale, foreign, multi-ref, replay, handler")


if __name__ == "__main__":
    main()
