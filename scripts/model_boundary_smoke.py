#!/usr/bin/env python3
"""Deterministic smoke for the real-model Living Context boundary.

This does not call a provider.  It feeds model-shaped JSON through the same
strict parser used by ``UnderstandingCore`` and proves that the narrow
normalizer repairs only its allowlisted compatibility cases.  A provider
acceptance run is intentionally kept separate because it is network- and
credential-dependent.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.living_context_candidate_extractor import extract_living_context_candidate  # noqa: E402
from core.living_reaction_feedback_extractor import (  # noqa: E402
    extract_living_reaction_feedback,
)
from core.understanding_core import UnderstandingCore  # noqa: E402
from interface.living_context_contract import (  # noqa: E402
    LivingContextCandidate,
    parse_living_context_candidate_detailed,
    parse_living_reaction_feedback_detailed,
    quarantine_typed_known_rows,
    quarantine_typed_timeline_rows,
    situation_catalog_selector,
)


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS {label}")


class FakeModelClient:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = [copy.deepcopy(payload) for payload in payloads]
        self.purposes: list[str] = []
        self.users: list[dict[str, object]] = []
        self.call_count = 0

    def complete_json(self, *, purpose: str, system: str, user: str) -> dict[str, object]:
        del system
        self.purposes.append(purpose)
        self.users.append(json.loads(user))
        self.call_count += 1
        if self.payloads:
            return copy.deepcopy(self.payloads.pop(0))
        return {"status": "unconfigured", "purpose": purpose}


class RaisingFakeModelClient(FakeModelClient):
    """Fake transport that proves client exceptions stay inside the seam."""

    def __init__(self, payloads: list[dict[str, object]], failing_purposes: set[str]) -> None:
        super().__init__(payloads)
        self.failing_purposes = set(failing_purposes)

    def complete_json(self, *, purpose: str, system: str, user: str) -> dict[str, object]:
        if purpose in self.failing_purposes:
            del system
            self.purposes.append(purpose)
            self.users.append(json.loads(user))
            self.call_count += 1
            raise RuntimeError("provider-secret-text-must-not-escape")
        return super().complete_json(purpose=purpose, system=system, user=user)


class FakeReasoning:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.client = FakeModelClient(payloads)
        self.traces: list[dict[str, object]] = []

    def is_enabled(self) -> bool:
        return True

    def _trace(self, purpose: str, result: dict[str, object], summary: dict[str, object]) -> None:
        del purpose, summary
        self.traces.append(copy.deepcopy(result))


def semantic_frame(text: str) -> dict[str, object]:
    quote = {"text": text, "start": 0, "end": len(text)}
    return {
        "schema_version": "veyra.semantic_frame.v1",
        "acts": [
            {
                "act_id": "a1",
                "kind": "statement",
                "goal": "record the reported Situation",
                "operation": "report",
                "target": {"type": "situation", "value": "上海出差", "attributes": {}},
                "polarity": "positive",
                "explicitness": "explicit",
                "source_quote": quote,
                "speaker": "user",
                "authority": "direct_user",
                "mention_mode": "normal_use",
                "evidence_need": "none",
                "referent": {
                    "surface": "",
                    "resolved": "",
                    "status": "not_applicable",
                    "candidates": [],
                },
                "condition": None,
                "modality": "asserted",
                "arguments": {},
            }
        ],
        "relations": [],
        "ambiguities": [],
        "resolver_status": "resolved",
        "source": "model",
    }


def candidate_payload(*, category: object = "出行") -> dict[str, object]:
    return {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": "创建",
        "create_subject": "上海出差",
        "category": category,
        "label": None,
        "title": None,
        "summary": None,
        "goal": "完成上海出差安排",
        "lifecycle": "进行中",
        "progress": {"status": "进行中", "value": 0.2},
        "entities": [{"kind": "地点", "value": "上海", "epistemic_status": "报告"}],
        "known": [{"statement": "用户报告下周去上海出差", "epistemic_status": "报告"}],
        "unknown": ["住宿安排"],
        "assumptions": ["住宿可能需要尽快确认"],
        "timeline": [],
        "material_change": None,
        "next_step": None,
        "next_step_epistemic_status": "推断",
        "needs": [
            {
                "blocked_judgment": "住宿安排",
                "evidence_kind": "用户输入",
                "why_now": "它会改变出差准备",
                "urgency": 0.6,
                "expires_at": None,
                "allowed_source_classes": ["user"],
                "fallback_reaction": "询问",
                "question": None,
            }
        ],
        "answered_need_tokens": [],
        "answered_need_bindings": [],
        "requested_reaction": "询问",
        "reopen": False,
        "reopen_reason": None,
        "source_quote": None,
        "assertion_mode": "推断",
        "source": "model",
    }


def model_payload(candidate: dict[str, object], *, text: str, feedback: dict[str, object] | None = None) -> dict[str, object]:
    situation: dict[str, object] = {
        "intent": "conversation",
        "task_type": "chat",
        "task_summary": text,
        "explicit_request": text,
        "user_goal": "保持生活 Situation 上下文",
        "confidence": 0.8,
        "living_context_candidate": candidate,
    }
    if feedback is not None:
        situation["living_reaction_feedback"] = feedback
    return {
        "status": "model_assisted",
        "source": "model",
        "situation_assessment": situation,
        "semantic_frame": semantic_frame(text),
    }


def build_understanding(payloads: list[dict[str, object]], text: str):
    return UnderstandingCore(FakeReasoning(payloads)).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": []},
    )


def main() -> int:
    text = "下周我要去上海出差"
    feedback = {
        "schema_version": "veyra.living_reaction_feedback.v1",
        "reaction_token": "rxn_audit",
        "label": "useful",
        "remind_before_seconds": None,
        "source_quote": {"text": text, "start": 0, "end": len(text)},
    }

    repaired = build_understanding(
        [model_payload(candidate_payload(), text=text, feedback=feedback)],
        text,
    )
    expect(repaired.source == "model", "allowlisted model output remains model-assisted")
    expect(repaired.living_context_candidate is not None, "null/default and exact enum aliases repair candidate")
    expect(
        repaired.living_context_candidate is not None
        and repaired.living_context_candidate.category == "travel"
        and repaired.living_context_candidate.needs[0].evidence_kind == "user"
        and repaired.living_context_candidate.needs[0].fallback_reaction == "ask",
        "known enum aliases become typed values",
    )
    expect(
        repaired.living_context_candidate is not None
        and repaired.living_context_candidate.assumptions[0].epistemic_status == "inferred",
        "assumption string becomes an inferred assumption, never a reported fact",
    )
    expect(repaired.living_reaction_feedback is not None, "feedback parses independently of candidate")
    expect(
        repaired.living_context_candidate_metrics.get("status") == "repaired"
        and repaired.living_reaction_feedback_metrics.get("status") == "accepted",
        "quality metrics distinguish repaired candidate and accepted feedback",
    )
    root_evidence_candidate = copy.deepcopy(candidate_payload(category="travel"))
    root_evidence_candidate["evidence_kind"] = "calendar"
    root_evidence_parsed, root_evidence_issues, root_evidence_report = (
        parse_living_context_candidate_detailed(root_evidence_candidate)
    )
    expect(
        root_evidence_parsed is not None
        and not root_evidence_issues
        and root_evidence_parsed.needs[0].evidence_kind == "user"
        and "evidence_kind:omitted_root_transport"
        in root_evidence_report.get("repaired_fields", [])
        and root_evidence_report.get("normalized_fields") == ["evidence_kind"],
        "root Need-only evidence_kind is discarded without migrating or inferring a source",
    )
    root_evidence_extracted = extract_living_context_candidate(
        FakeModelClient(
            [
                {
                    "status": "model_assisted",
                    "living_context_candidate": root_evidence_candidate,
                }
            ]
        ),
        text=text,
    )
    expect(
        root_evidence_extracted.candidate is not None
        and root_evidence_extracted.metrics.get("normalized_fields") == ["evidence_kind"]
        and root_evidence_extracted.metrics.get("normalization_status") == "normalized"
        and "calendar"
        not in json.dumps(
            root_evidence_extracted.candidate.model_dump(mode="json"),
            ensure_ascii=False,
        ),
        "candidate extractor records bounded root evidence_kind normalization",
    )
    root_evidence_understanding = UnderstandingCore(
        FakeReasoning([model_payload(root_evidence_candidate, text=text)])
    ).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": []},
    )
    expect(
        root_evidence_understanding.living_context_candidate is not None
        and root_evidence_understanding.living_context_candidate.needs[0].evidence_kind
        == "user",
        "UnderstandingCore shared path discards root evidence_kind without source migration",
    )
    unknown_root_extra = copy.deepcopy(candidate_payload(category="travel"))
    unknown_root_extra["unrelated_extra"] = "must remain rejected"
    unknown_root_parsed, unknown_root_issues, _unknown_root_report = (
        parse_living_context_candidate_detailed(unknown_root_extra)
    )
    expect(
        unknown_root_parsed is None
        and any("extra_forbidden" in issue for issue in unknown_root_issues),
        "other unknown root extras remain strictly rejected",
    )
    defaulted_create = copy.deepcopy(candidate_payload(category="travel"))
    defaulted_create["goal"] = None
    defaulted_create["requested_reaction"] = None
    defaulted_create["next_step_epistemic_status"] = None
    defaulted_create["reopen"] = None
    defaulted_create["known"] = None
    defaulted_create["situation_token"] = ""
    defaulted_create["situation_revision"] = 0
    defaulted_create["catalog_token"] = None
    defaulted_candidate, defaulted_issues, defaulted_report = parse_living_context_candidate_detailed(
        defaulted_create,
    )
    expect(
        defaulted_candidate is not None
        and not defaulted_issues
        and defaulted_candidate.goal == defaulted_candidate.create_subject
        and defaulted_candidate.requested_reaction == "wait"
        and defaulted_candidate.next_step_epistemic_status == "inferred"
        and defaulted_candidate.reopen is False
        and defaulted_candidate.known == []
        and defaulted_candidate.situation_token is None
        and defaulted_candidate.situation_revision is None
        and defaulted_candidate.catalog_token is None
        and "goal:from_create_subject" in defaulted_report["repaired_fields"],
        "null optional defaults and create goal bootstrap remain explicit repairs",
    )
    aliased_create = copy.deepcopy(defaulted_create)
    aliased_create["disposition"] = "新建"
    aliased_candidate, aliased_issues, aliased_report = parse_living_context_candidate_detailed(
        aliased_create,
    )
    expect(
        aliased_candidate is not None
        and not aliased_issues
        and aliased_candidate.disposition == "create"
        and "disposition" in aliased_report["repaired_fields"]
        and "situation_revision:create_absent" in aliased_report["repaired_fields"],
        "create aliases receive create transport defaults before strict validation",
    )
    create_with_old_binding = copy.deepcopy(defaulted_create)
    create_with_old_binding.update(
        {
            "situation_token": "sit_old_binding",
            "situation_revision": 7,
            "catalog_token": "cat_old_binding",
        }
    )
    cleared_create, cleared_create_issues, cleared_create_report = parse_living_context_candidate_detailed(
        create_with_old_binding,
    )
    expect(
        cleared_create is not None
        and not cleared_create_issues
        and cleared_create.disposition == "create"
        and cleared_create.situation_token is None
        and cleared_create.situation_revision is None
        and cleared_create.catalog_token is None
        and all(
            marker in cleared_create_report["repaired_fields"]
            for marker in (
                "situation_token:create_binding_cleared",
                "situation_revision:create_binding_cleared",
                "catalog_token:create_binding_cleared",
            )
        ),
        "create clears stale Situation bindings without catalog selection",
    )
    existing_binding = copy.deepcopy(candidate_payload(category="travel"))
    existing_binding.update(
        {
            "disposition": "update",
            "situation_token": "sit_existing_exact",
            "situation_revision": 3,
            "catalog_token": "cat_existing_exact",
        }
    )
    existing_candidate, existing_issues, _ = parse_living_context_candidate_detailed(
        existing_binding,
    )
    expect(
        existing_candidate is not None
        and not existing_issues
        and existing_candidate.disposition == "update"
        and existing_candidate.situation_token == "sit_existing_exact"
        and existing_candidate.situation_revision == 3
        and existing_candidate.catalog_token == "cat_existing_exact",
        "existing Situation dispositions preserve exact bindings",
    )
    selector_catalog = {
        "owner_id": "selector-owner",
        "session_id": "selector-session",
        "situation_token": "sit_selector_demo",
        "situation_revision": 8,
        "catalog_token": "cat_selector_current",
    }
    selector_catalog["situation_selector"] = situation_catalog_selector(
        selector_catalog["owner_id"],
        selector_catalog["session_id"],
        selector_catalog["situation_token"],
    )
    selector_candidate = copy.deepcopy(candidate_payload(category="travel"))
    selector_candidate.update(
        {
            "disposition": "update",
            "situation_selector": selector_catalog["situation_selector"],
            "situation_token": "sit_old_revision_copy",
            "situation_revision": 2,
            "catalog_token": "cat_old_revision_copy",
        }
    )
    selector_parsed, selector_issues, selector_report = parse_living_context_candidate_detailed(
        selector_candidate,
        catalog=[selector_catalog],
    )
    expect(
        selector_parsed is not None
        and not selector_issues
        and selector_parsed.situation_token == selector_catalog["situation_token"]
        and selector_parsed.situation_revision == selector_catalog["situation_revision"]
        and selector_parsed.catalog_token == selector_catalog["catalog_token"]
        and "situation_binding:from_selector" in selector_report["repaired_fields"],
        "unique server selector canonicalizes one current binding triple",
    )
    stale_selector = copy.deepcopy(selector_candidate)
    stale_selector["situation_selector"] = situation_catalog_selector(
        "selector-owner",
        "selector-session",
        "sit_removed_from_current_catalog",
    )
    stale_parsed, stale_issues, _ = parse_living_context_candidate_detailed(
        stale_selector,
        catalog=[selector_catalog],
    )
    expect(
        stale_parsed is None
        and stale_issues == ["living_context_candidate:binding:situation_selector_not_unique"],
        "stale selector remains fail-closed even with plausible old bindings",
    )
    ambiguous_selector = dict(selector_catalog)
    ambiguous_selector["situation_revision"] = 9
    ambiguous_parsed, ambiguous_issues, _ = parse_living_context_candidate_detailed(
        selector_candidate,
        catalog=[selector_catalog, ambiguous_selector],
    )
    expect(
        ambiguous_parsed is None
        and ambiguous_issues == ["living_context_candidate:binding:situation_selector_not_unique"],
        "duplicate selector rows remain fail-closed rather than semantically selected",
    )
    selector_understanding = UnderstandingCore(
        FakeReasoning([model_payload(selector_candidate, text=text)])
    ).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": [selector_catalog]},
    )
    expect(
        selector_understanding.living_context_candidate is not None
        and selector_understanding.living_context_candidate.situation_token
        == selector_catalog["situation_token"]
        and selector_understanding.living_context_candidate.situation_revision
        == selector_catalog["situation_revision"],
        "combined understanding resolves a selector before the strict candidate boundary",
    )
    material_text = copy.deepcopy(candidate_payload(category="travel"))
    material_text["material_change"] = "航班时间已确认"
    material_text_candidate, material_text_issues, _ = parse_living_context_candidate_detailed(
        material_text,
    )
    expect(
        material_text_candidate is not None
        and not material_text_issues
        and material_text_candidate.material_change == "航班时间已确认",
        "material_change preserves valid display text",
    )
    for no_value in (None, ""):
        material_empty = copy.deepcopy(candidate_payload(category="travel"))
        material_empty["material_change"] = no_value
        material_empty_candidate, material_empty_issues, _ = parse_living_context_candidate_detailed(
            material_empty,
        )
        expect(
            material_empty_candidate is not None
            and not material_empty_issues
            and material_empty_candidate.material_change == "",
            "material_change null and empty values remain absent",
        )
    for invalid_material in ({"changed": True}, ["changed"], 42):
        malformed_material = copy.deepcopy(candidate_payload(category="travel"))
        malformed_material["material_change"] = invalid_material
        malformed_material_candidate, malformed_material_issues, malformed_material_report = (
            parse_living_context_candidate_detailed(malformed_material)
        )
        expect(
            malformed_material_candidate is not None
            and not malformed_material_issues
            and malformed_material_candidate.material_change == ""
            and "material_change:omitted_invalid_transport"
            in malformed_material_report["repaired_fields"],
            "invalid material_change transport is omitted without stringification",
        )
    integral_numbers = copy.deepcopy(candidate_payload(category="travel"))
    integral_numbers["progress"] = {"status": "in_progress", "value": "1"}
    integral_numbers["needs"][0]["urgency"] = 1
    numeric_candidate, numeric_issues, numeric_report = parse_living_context_candidate_detailed(
        integral_numbers,
    )
    expect(
        numeric_candidate is not None
        and not numeric_issues
        and numeric_report["status"] == "repaired"
        and "progress.value" in numeric_report["repaired_fields"]
        and "needs[0].urgency" in numeric_report["repaired_fields"],
        "integral JSON numbers receive only lossless float repairs",
    )
    for invalid_value in ("not-a-number", "50%", "NaN", float("nan")):
        invalid_ratio = copy.deepcopy(candidate_payload(category="travel"))
        invalid_ratio["progress"] = {"status": "in_progress", "value": invalid_value}
        ratio_candidate, ratio_issues, ratio_report = parse_living_context_candidate_detailed(
            invalid_ratio,
        )
        expect(
            ratio_candidate is not None
            and not ratio_issues
            and ratio_candidate.progress.value is None
            and "progress.value:omitted_invalid_transport" in ratio_report["repaired_fields"],
            "invalid optional progress ratio is omitted without guessing",
        )
    singleton_known = copy.deepcopy(candidate_payload(category="travel"))
    singleton_known["known"] = singleton_known["known"][0]
    singleton_candidate, singleton_issues, singleton_report = parse_living_context_candidate_detailed(
        singleton_known,
    )
    expect(
        singleton_candidate is not None
        and not singleton_issues
        and len(singleton_candidate.known) == 1
        and "known:singleton_list" in singleton_report["repaired_fields"],
        "exact one-item collection objects receive a lossless list repair",
    )
    malformed_singleton = copy.deepcopy(candidate_payload(category="travel"))
    malformed_singleton["known"] = {
        **malformed_singleton["known"][0],
        "unexpected": "not part of the known-item contract",
    }
    rejected_singleton, rejected_singleton_issues, _ = parse_living_context_candidate_detailed(
        malformed_singleton,
    )
    expect(
        rejected_singleton is None
        and any("known:list_type" in issue for issue in rejected_singleton_issues),
        "one-item collection repair never hides unknown fields",
    )
    malicious_extra_key = "unknown_sk-SECRET_TOKEN"
    malicious_candidate = copy.deepcopy(candidate_payload(category="travel"))
    malicious_candidate[malicious_extra_key] = "provider-controlled value"
    malicious_parsed, malicious_issues, _malicious_report = (
        parse_living_context_candidate_detailed(malicious_candidate)
    )
    expect(
        malicious_parsed is None
        and malicious_issues == [
            "living_context_candidate:extra_field:extra_forbidden"
        ]
        and malicious_extra_key not in json.dumps(malicious_issues, ensure_ascii=False)
        and "SECRET_TOKEN" not in json.dumps(malicious_issues, ensure_ascii=False),
        "unknown candidate extra keys are replaced with a fixed safe issue location",
    )
    malicious_feedback = {
        **feedback,
        malicious_extra_key: "feedback-controlled value",
    }
    malicious_feedback_parsed, malicious_feedback_issues, _malicious_feedback_report = (
        parse_living_reaction_feedback_detailed(
            malicious_feedback,
            source_text=text,
        )
    )
    expect(
        malicious_feedback_parsed is None
        and malicious_feedback_issues == [
            "living_reaction_feedback:extra_field:extra_forbidden"
        ]
        and malicious_extra_key not in json.dumps(
            malicious_feedback_issues,
            ensure_ascii=False,
        )
        and "SECRET_TOKEN" not in json.dumps(
            malicious_feedback_issues,
            ensure_ascii=False,
        ),
        "unknown feedback extra keys use the same safe shared issue encoding",
    )
    nested_malicious_candidate = copy.deepcopy(candidate_payload(category="travel"))
    nested_malicious_candidate["needs"][0][malicious_extra_key] = "nested secret"
    nested_malicious_parsed, nested_malicious_issues, _nested_malicious_report = (
        parse_living_context_candidate_detailed(nested_malicious_candidate)
    )
    expect(
        nested_malicious_parsed is None
        and nested_malicious_issues == [
            "living_context_candidate:needs.0.extra_field:extra_forbidden"
        ]
        and malicious_extra_key not in json.dumps(nested_malicious_issues, ensure_ascii=False),
        "nested candidate extra keys preserve safe fields and integer indices only",
    )
    nested_malicious_feedback = copy.deepcopy(feedback)
    nested_malicious_feedback["source_quote"][malicious_extra_key] = "nested secret"
    nested_feedback_parsed, nested_feedback_issues, _nested_feedback_report = (
        parse_living_reaction_feedback_detailed(
            nested_malicious_feedback,
            source_text=text,
        )
    )
    expect(
        nested_feedback_parsed is None
        and nested_feedback_issues == [
            "living_reaction_feedback:source_quote.extra_field:extra_forbidden"
        ]
        and malicious_extra_key not in json.dumps(nested_feedback_issues, ensure_ascii=False),
        "nested feedback extra keys preserve known paths without leaking names",
    )
    for provenance in ("user", None, "provider_literal"):
        provenance_candidate = copy.deepcopy(candidate_payload(category="travel"))
        provenance_candidate["source"] = provenance
        server_attested, provenance_issues, provenance_report = parse_living_context_candidate_detailed(
            provenance_candidate,
        )
        expect(
            server_attested is not None
            and not provenance_issues
            and server_attested.source == "model"
            and provenance_report["status"] == "repaired"
            and "source" in provenance_report["repaired_fields"],
            "candidate provenance is server-attested as model with a bounded repair marker",
        )
    question_bound_need = copy.deepcopy(candidate_payload(category="travel"))
    question_bound_need["needs"][0].pop("blocked_judgment")
    question_bound_need["needs"][0]["question"] = "需要确认同一事项的下一步"
    question_candidate, question_issues, question_report = parse_living_context_candidate_detailed(
        question_bound_need,
    )
    expect(
        question_candidate is not None
        and not question_issues
        and question_candidate.needs[0].blocked_judgment == "需要确认同一事项的下一步"
        and "needs[0].blocked_judgment:from_question" in question_report["repaired_fields"],
        "Need blocked judgment copies only its own non-empty question",
    )
    missing_need_judgment = copy.deepcopy(candidate_payload(category="travel"))
    missing_need_judgment["needs"][0].pop("blocked_judgment")
    missing_need_judgment["needs"][0]["question"] = ""
    missing_need_candidate, missing_need_issues, _missing_need_report = parse_living_context_candidate_detailed(
        missing_need_judgment,
    )
    expect(
        missing_need_candidate is None
        and any("needs.0.blocked_judgment:missing" in issue for issue in missing_need_issues),
        "Need missing both blocked judgment and question remains rejected",
    )
    token_only_update = copy.deepcopy(candidate_payload(category="travel"))
    token_only_update.update(
        {
            "disposition": "update",
            "situation_token": "sit_boundary_demo",
            "situation_revision": 1,
            "catalog_token": "cat_boundary_demo",
            "answered_need_tokens": ["need_not_bound"],
            "answered_need_bindings": [],
        }
    )
    token_only_candidate, token_only_issues, _ = parse_living_context_candidate_detailed(
        token_only_update,
    )
    expect(
        token_only_candidate is None
        and any("need_binding_mismatch" in issue for issue in token_only_issues),
        "answered Need tokens require an exact binding set",
    )
    duplicate_tokens = copy.deepcopy(token_only_update)
    duplicate_tokens["answered_need_tokens"] = ["need_not_bound", "need_not_bound"]
    duplicate_tokens["answered_need_bindings"] = [
        {"need_token": "need_not_bound", "generation": 1}
    ]
    duplicate_candidate, duplicate_issues, _ = parse_living_context_candidate_detailed(
        duplicate_tokens,
    )
    expect(
        duplicate_candidate is None
        and any("need_binding_mismatch" in issue for issue in duplicate_issues),
        "answered Need tokens and bindings remain one-to-one without duplicates",
    )
    quiet_mutation = {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": "quiet",
        "source": "model",
        "summary": "quiet must not carry a Situation mutation",
    }
    quiet_mutation_candidate, quiet_mutation_issues, _ = parse_living_context_candidate_detailed(
        quiet_mutation,
    )
    expect(
        quiet_mutation_candidate is None
        and quiet_mutation_issues == ["living_context_candidate:quiet_mutation_conflict"],
        "quiet candidate rejects every non-minimal mutation field",
    )
    empty_sources = copy.deepcopy(candidate_payload(category="travel"))
    empty_sources["needs"][0]["allowed_source_classes"] = []
    empty_sources_candidate, empty_sources_issues, _ = parse_living_context_candidate_detailed(
        empty_sources,
    )
    expect(
        empty_sources_candidate is None
        and any("allowed_source_classes:too_short" in issue for issue in empty_sources_issues),
        "every InformationNeed requires at least one registered source class",
    )
    evidence_alias_sources = copy.deepcopy(candidate_payload(category="travel"))
    evidence_alias_sources["needs"][0]["allowed_source_classes"] = [
        "用户",
        "日历",
        "网页",
    ]
    evidence_alias_candidate, evidence_alias_issues, _ = parse_living_context_candidate_detailed(
        evidence_alias_sources,
    )
    expect(
        evidence_alias_candidate is not None
        and not evidence_alias_issues
        and evidence_alias_candidate.needs[0].allowed_source_classes
        == ["user", "calendar", "public_web"],
        "Need source classes reuse exact evidence-kind aliases",
    )
    transport_alias_source = copy.deepcopy(candidate_payload(category="travel"))
    transport_alias_source["needs"][0]["allowed_source_classes"] = ["user_message"]
    transport_alias_candidate, transport_alias_issues, _ = parse_living_context_candidate_detailed(
        transport_alias_source,
    )
    expect(
        transport_alias_candidate is not None
        and not transport_alias_issues
        and transport_alias_candidate.needs[0].allowed_source_classes == ["user"],
        "Need source classes retain exact transport aliases",
    )

    mixed_source_candidate = copy.deepcopy(candidate_payload(category="travel"))
    mixed_source_candidate["needs"][0]["allowed_source_classes"] = [
        "calendar",
        "provider_specific_unknown",
        "user",
    ]
    mixed_source_result = extract_living_context_candidate(
        FakeModelClient(
            [
                {
                    "status": "model_assisted",
                    "living_context_candidate": mixed_source_candidate,
                }
            ]
        ),
        text=text,
    )
    expect(
        mixed_source_result.candidate is not None
        and mixed_source_result.candidate.needs[0].allowed_source_classes
        == ["calendar", "user"]
        and mixed_source_result.metrics.get("dropped_unsupported_count") == 1
        and "needs[0].allowed_source_classes"
        in mixed_source_result.metrics.get("normalized_fields", []),
        "registered source classes survive a stable intersection with unknown values dropped",
    )

    alias_mixed_source_candidate = copy.deepcopy(candidate_payload(category="travel"))
    alias_mixed_source_candidate["needs"][0]["allowed_source_classes"] = [
        "user_message",
        "provider_specific_unknown",
    ]
    alias_mixed_source_result = extract_living_context_candidate(
        FakeModelClient(
            [
                {
                    "status": "model_assisted",
                    "living_context_candidate": alias_mixed_source_candidate,
                }
            ]
        ),
        text=text,
    )
    expect(
        alias_mixed_source_result.candidate is not None
        and alias_mixed_source_result.candidate.needs[0].allowed_source_classes
        == ["user"]
        and alias_mixed_source_result.metrics.get("dropped_unsupported_count") == 1
        and "provider_specific_unknown"
        not in json.dumps(
            alias_mixed_source_result.candidate.model_dump(mode="json"),
            ensure_ascii=False,
        ),
        "source aliases normalize before unknown values are dropped without widening access",
    )

    all_unknown_source_candidate = copy.deepcopy(candidate_payload(category="travel"))
    all_unknown_source_candidate["needs"][0]["allowed_source_classes"] = [
        "provider_specific_unknown",
        "another_unknown",
    ]
    all_unknown_source_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": all_unknown_source_candidate,
            },
            {
                "status": "model_assisted",
                "living_context_candidate": all_unknown_source_candidate,
            },
        ]
    )
    all_unknown_source_result = extract_living_context_candidate(
        all_unknown_source_client,
        text=text,
    )
    all_unknown_repair_request = json.dumps(
        all_unknown_source_client.users[1],
        ensure_ascii=False,
    )
    expect(
        all_unknown_source_result.candidate is None
        and all_unknown_source_result.metrics.get("status") == "rejected"
        and all_unknown_source_result.issues
        == ["living_context_candidate:root:unsupported_source_class"]
        and "provider_specific_unknown" not in all_unknown_repair_request
        and "another_unknown" not in all_unknown_repair_request,
        "all unsupported source classes reject and never enter the repair provider request",
    )

    source_shape_cases = {
        "non_list": "user",
        "oversized": ["user", "calendar", "email", "weather"],
        "wrong_item_type": ["user", 7],
    }
    for source_shape, source_value in source_shape_cases.items():
        malformed_source_candidate = copy.deepcopy(candidate_payload(category="travel"))
        malformed_source_candidate["needs"][0]["allowed_source_classes"] = source_value
        malformed_source_client = FakeModelClient(
            [
                {
                    "status": "model_assisted",
                    "living_context_candidate": malformed_source_candidate,
                },
                {
                    "status": "model_assisted",
                    "living_context_candidate": malformed_source_candidate,
                },
            ]
        )
        malformed_source_result = extract_living_context_candidate(
            malformed_source_client,
            text=text,
        )
        expect(
            malformed_source_result.candidate is None
            and malformed_source_result.metrics.get("status") == "rejected"
            and malformed_source_client.call_count == 2,
            f"source class {source_shape} remains strict after bounded repair",
        )

    quiet_text = "补充原事项进展"
    quiet_candidate = {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": "quiet",
        "source": "model",
    }
    update_candidate = {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": "update",
        "situation_token": "sit_boundary_demo",
        "situation_revision": 1,
        "catalog_token": "cat_boundary_demo",
        "summary": "原事项已有新的进展",
        "source": "model",
    }
    continuation = UnderstandingCore(
        FakeReasoning(
            [
                model_payload(quiet_candidate, text=quiet_text),
                {
                    "status": "model_assisted",
                    "living_context_candidate": update_candidate,
                },
            ]
        )
    ).build(
        text=quiet_text,
        attention_focus=[],
        turn_context={
            "living_context_situation_candidates": [
                {
                    "situation_token": "sit_boundary_demo",
                    "situation_revision": 1,
                    "catalog_token": "cat_boundary_demo",
                }
            ]
        },
    )
    expect(
        continuation.living_context_candidate is not None
        and continuation.living_context_candidate.disposition == "update"
        and continuation.model_boundary_metrics.get("repair_attempted") is True,
        "explicit Situation continuation retries a quiet model candidate once",
    )

    missing_candidate_payload = model_payload(candidate_payload(), text=text)
    missing_candidate_payload["situation_assessment"].pop("living_context_candidate")
    extractor_reasoning = FakeReasoning(
        [
            missing_candidate_payload,
            {
                "status": "model_assisted",
                "living_context_candidate": candidate_payload(category="travel"),
            },
        ]
    )
    extracted = UnderstandingCore(extractor_reasoning).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": []},
    )
    expect(
        extracted.living_context_candidate is not None
        and extracted.living_context_candidate.disposition == "create"
        and extracted.model_boundary_metrics.get("candidate_extraction", {}).get("extractor_attempted") is True,
        "missing Situation candidate uses one strict candidate-only fallback",
    )
    expect(
        extractor_reasoning.client.purposes == [
            "turn_understanding",
            "living_context_candidate_extraction",
        ],
        "candidate-only fallback is bounded and does not invoke a second full repair",
    )

    transport_fallback_reasoning = FakeReasoning(
        [
            {"status": "error", "purpose": "turn_understanding"},
            {
                "status": "model_assisted",
                "living_context_candidate": candidate_payload(category="travel"),
            },
        ]
    )
    transport_recovered = UnderstandingCore(transport_fallback_reasoning).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": []},
    )
    expect(
        transport_recovered.source == "model_candidate"
        and transport_recovered.living_context_candidate is not None
        and transport_recovered.living_context_candidate.disposition == "create"
        and transport_recovered.model_boundary_metrics.get("combined_understanding_available") is False,
        "combined understanding transport loss keeps one strict candidate-only recovery",
    )
    expect(
        transport_fallback_reasoning.client.purposes == [
            "turn_understanding",
            "living_context_candidate_extraction",
        ],
        "transport recovery remains bounded to one smaller model seam",
    )

    full_repair_failure_reasoning = FakeReasoning(
        [
            {"status": "invalid_json"},
            {"status": "invalid_response"},
            {
                "status": "model_assisted",
                "living_context_candidate": candidate_payload(category="travel"),
            },
        ]
    )
    full_repair_failure = UnderstandingCore(full_repair_failure_reasoning).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": []},
    )
    expect(
        full_repair_failure.living_context_candidate is not None
        and full_repair_failure.source == "model_candidate"
        and full_repair_failure_reasoning.client.purposes == [
            "turn_understanding",
            "turn_understanding_repair",
            "living_context_candidate_extraction",
        ],
        "failed full understanding repair receives one bounded candidate recovery",
    )
    expect(
        full_repair_failure.model_boundary_metrics.get("repair_attempted") is True
        and full_repair_failure.model_boundary_metrics.get("combined_understanding_status")
        == "invalid_json"
        and full_repair_failure.model_boundary_metrics.get("full_repair_status")
        == "invalid_response",
        "failed full repair metrics remain explicit and bounded",
    )

    initial_exception_reasoning = FakeReasoning([])
    initial_exception_reasoning.client = RaisingFakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": candidate_payload(category="travel"),
            }
        ],
        {"turn_understanding"},
    )
    initial_exception = UnderstandingCore(initial_exception_reasoning).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": []},
    )
    initial_exception_dump = json.dumps(
        {
            "understanding": initial_exception.to_dict(include_raw=True),
            "traces": initial_exception_reasoning.traces,
        },
        ensure_ascii=False,
    )
    expect(
        initial_exception.living_context_candidate is not None
        and initial_exception_reasoning.client.purposes == [
            "turn_understanding",
            "living_context_candidate_extraction",
        ]
        and "provider-secret-text-must-not-escape" not in initial_exception_dump,
        "full model exception is contained and uses the same candidate recovery",
    )

    repair_exception_reasoning = FakeReasoning([])
    repair_exception_reasoning.client = RaisingFakeModelClient(
        [
            {"status": "invalid_json"},
            {
                "status": "model_assisted",
                "living_context_candidate": candidate_payload(category="travel"),
            },
        ],
        {"turn_understanding_repair"},
    )
    repair_exception = UnderstandingCore(repair_exception_reasoning).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": []},
    )
    repair_exception_dump = json.dumps(
        {
            "understanding": repair_exception.to_dict(include_raw=True),
            "traces": repair_exception_reasoning.traces,
        },
        ensure_ascii=False,
    )
    expect(
        repair_exception.living_context_candidate is not None
        and repair_exception_reasoning.client.purposes == [
            "turn_understanding",
            "turn_understanding_repair",
            "living_context_candidate_extraction",
        ]
        and repair_exception.model_boundary_metrics.get("full_repair_status") == "exception"
        and "provider-secret-text-must-not-escape" not in repair_exception_dump,
        "full repair exception is contained without persisting exception text",
    )

    post_repair_primary = model_payload(quiet_candidate, text=text)
    post_repair_primary["semantic_frame"]["acts"][0]["source_quote"] = {
        "text": "not in the user turn",
        "start": 0,
        "end": 17,
    }
    post_repair_missing = model_payload(candidate_payload(category="travel"), text=text)
    post_repair_missing["situation_assessment"].pop("living_context_candidate")
    post_repair_reasoning = FakeReasoning(
        [
            post_repair_primary,
            post_repair_missing,
            {
                "status": "model_assisted",
                "living_context_candidate": candidate_payload(category="travel"),
            },
        ]
    )
    post_repair_extracted = UnderstandingCore(post_repair_reasoning).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": []},
    )
    expect(
        post_repair_extracted.living_context_candidate is not None
        and post_repair_extracted.living_context_candidate.disposition == "create"
        and post_repair_reasoning.client.purposes == [
            "turn_understanding",
            "turn_understanding_repair",
            "living_context_candidate_extraction",
        ],
        "post-full-repair missing candidate uses the shared extractor once",
    )

    post_repair_feedback = model_payload(quiet_candidate, text=text, feedback=feedback)
    post_repair_feedback["situation_assessment"].pop("living_context_candidate")
    forbidden_extractor_response = {
        "status": "model_assisted",
        "living_context_candidate": {
            **candidate_payload(category="travel"),
            "route": "agent",
        },
    }
    post_repair_feedback_reasoning = FakeReasoning(
        [post_repair_primary, post_repair_feedback, forbidden_extractor_response]
    )
    post_repair_feedback_result = UnderstandingCore(post_repair_feedback_reasoning).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": []},
    )
    expect(
        post_repair_feedback_result.living_context_candidate is None
        and post_repair_feedback_result.living_reaction_feedback is not None
        and post_repair_feedback_result.living_context_candidate_metrics.get("issue_codes")
        == ["living_context_candidate:extractor:forbidden_field"],
        "post-full-repair extractor rejection surfaces metrics and preserves feedback",
    )
    expect(
        post_repair_feedback_reasoning.client.call_count == 3
        and post_repair_feedback_reasoning.client.purposes == [
            "turn_understanding",
            "turn_understanding_repair",
            "living_context_candidate_extraction",
        ],
        "post-full-repair recovery has no duplicate semantic repair or infinite calls",
    )

    envelope_extraction = extract_living_context_candidate(
        FakeModelClient(
            [
                {
                    "status": "model_assisted",
                    "living_context_candidate": candidate_payload(category="travel"),
                }
            ]
        ),
        text=text,
    )
    expect(
        envelope_extraction.candidate is not None
        and envelope_extraction.metrics.get("status") in {"accepted", "repaired"},
        "candidate extractor accepts its explicit envelope contract",
    )

    # Timeline is intentionally strict at the candidate boundary: a mapping,
    # scalar, oversized collection, or malformed row must take the bounded
    # candidate-only repair path rather than being silently interpreted by the
    # server.  The repair request is a small schema-shaped projection, not a
    # replay of the full understanding prompt.
    timeline_repaired = copy.deepcopy(candidate_payload(category="travel"))
    timeline_repaired["timeline"] = [
        {
            "statement": "用户报告下周去上海出差",
            "occurred_at": None,
            "material": True,
        }
    ]
    direct_bad_timeline = copy.deepcopy(candidate_payload(category="travel"))
    direct_bad_timeline["timeline"] = [
        {
            "statement": "直接 contract 中的坏时间线",
            "source_quote": {"text": "不在当前消息", "start": 0, "end": 6},
            "material": True,
        }
    ]
    direct_timeline_candidate, direct_timeline_issues, direct_timeline_report = (
        parse_living_context_candidate_detailed(
            direct_bad_timeline,
            source_text=text,
        )
    )
    expect(
        direct_timeline_candidate is not None
        and not direct_timeline_issues
        and direct_timeline_candidate.timeline == []
        and direct_timeline_report.get("dropped_timeline_count") == 1,
        "shared contract parser quarantines a non-source-bound optional timeline row",
    )
    typed_timeline_candidate = LivingContextCandidate.model_validate(
        {
            "schema_version": "veyra.living_context_candidate.v1",
            "disposition": "create",
            "create_subject": "typed timeline boundary",
            "category": "general",
            "summary": "typed timeline boundary",
            "known": [],
            "unknown": [],
            "assumptions": [],
            "timeline": [
                {
                    "statement": "typed row with stale quote",
                    "source_quote": {
                        "text": "not in current turn",
                        "start": 0,
                        "end": 18,
                    },
                    "material": True,
                }
            ],
            "needs": [],
            "requested_reaction": "wait",
            "source": "model",
        },
        strict=True,
    )
    typed_timeline_filtered, typed_timeline_dropped = quarantine_typed_timeline_rows(
        typed_timeline_candidate,
        source_text=text,
    )
    expect(
        typed_timeline_filtered.timeline == []
        and typed_timeline_dropped == 1,
        "post-parse typed timeline quarantine removes a stale source quote row",
    )
    bad_known_row = {
        "statement": "坏 Known 行不应保留",
        "source_quote": {"text": "不在当前消息", "start": 0, "end": 6},
        "epistemic_status": "报告",
    }
    good_known_row = {
        "statement": "好 Known 行保持原有合同",
        "epistemic_status": "报告",
    }
    mixed_known_candidate = copy.deepcopy(candidate_payload(category="travel"))
    mixed_known_candidate["known"] = [bad_known_row, good_known_row]
    mixed_known_parsed, mixed_known_issues, mixed_known_report = (
        parse_living_context_candidate_detailed(
            mixed_known_candidate,
            source_text=text,
        )
    )
    expect(
        mixed_known_parsed is not None
        and not mixed_known_issues
        and [row.statement for row in mixed_known_parsed.known]
        == [good_known_row["statement"]]
        and mixed_known_report.get("dropped_known_count") == 1
        and "坏 Known 行不应保留"
        not in json.dumps(mixed_known_report, ensure_ascii=False),
        "one bad Known quote row is quarantined while one good row remains",
    )
    mixed_known_extracted = extract_living_context_candidate(
        FakeModelClient(
            [
                {
                    "status": "model_assisted",
                    "living_context_candidate": mixed_known_candidate,
                }
            ]
        ),
        text=text,
    )
    expect(
        mixed_known_extracted.candidate is not None
        and len(mixed_known_extracted.candidate.known) == 1
        and mixed_known_extracted.metrics.get("dropped_known_count") == 1
        and mixed_known_extracted.metrics.get("normalized_fields") == ["known"],
        "candidate extractor merges Known quarantine metrics without payload text",
    )
    all_bad_known_candidate = copy.deepcopy(candidate_payload(category="travel"))
    all_bad_known_candidate["known"] = [bad_known_row, dict(bad_known_row)]
    all_bad_known_parsed, all_bad_known_issues, all_bad_known_report = (
        parse_living_context_candidate_detailed(
            all_bad_known_candidate,
            source_text=text,
        )
    )
    expect(
        all_bad_known_parsed is not None
        and not all_bad_known_issues
        and all_bad_known_parsed.known == []
        and all_bad_known_report.get("dropped_known_count") == 2,
        "all bad Known quote rows quarantine to an empty optional projection",
    )
    typed_known_candidate = LivingContextCandidate.model_validate(
        {
            "schema_version": "veyra.living_context_candidate.v1",
            "disposition": "create",
            "create_subject": "typed Known boundary",
            "category": "general",
            "summary": "typed Known boundary",
            "known": [
                {
                    "statement": "typed Known with stale quote",
                    "source_quote": {
                        "text": "not in current turn",
                        "start": 0,
                        "end": 18,
                    },
                    "epistemic_status": "reported",
                }
            ],
            "timeline": [],
            "needs": [],
            "requested_reaction": "wait",
            "source": "model",
        },
        strict=True,
    )
    typed_known_filtered, typed_known_dropped = quarantine_typed_known_rows(
        typed_known_candidate,
        source_text=text,
    )
    expect(
        typed_known_filtered.known == []
        and typed_known_dropped == 1,
        "post-parse typed Known quarantine removes a stale quote row",
    )
    timeline_invalid_shapes: dict[str, object] = {
        "dict": {"statement": "错误的时间线容器", "unexpected": "provider field"},
        "string": "错误的时间线容器",
        "oversized": [{"statement": f"时间线项 {index}"} for index in range(13)],
        "invalid_row": [{"statement": 42}],
    }
    for shape_name, malformed_timeline in timeline_invalid_shapes.items():
        malformed_timeline_candidate = copy.deepcopy(candidate_payload(category="travel"))
        malformed_timeline_candidate["timeline"] = malformed_timeline
        timeline_client = FakeModelClient(
            [
                {
                    "status": "model_assisted",
                    "living_context_candidate": malformed_timeline_candidate,
                },
                {
                    "status": "model_assisted",
                    "living_context_candidate": timeline_repaired,
                },
            ]
        )
        timeline_result = extract_living_context_candidate(
            timeline_client,
            text=text,
        )
        if shape_name == "invalid_row":
            expect(
                timeline_result.candidate is not None
                and timeline_result.candidate.timeline == []
                and timeline_result.metrics.get("dropped_timeline_count") == 1
                and timeline_result.metrics.get("normalization_status") == "normalized"
                and timeline_client.call_count == 1,
                "invalid timeline row is quarantined without a semantic repair call",
            )
            continue
        timeline_repair_request = timeline_client.users[1]
        timeline_previous = timeline_repair_request.get("previous_candidate", {})
        expect(
            timeline_result.candidate is not None
            and timeline_result.candidate.timeline
            and timeline_result.metrics.get("status") == "repaired"
            and timeline_client.call_count == 2,
            f"timeline {shape_name} receives one bounded candidate-only repair",
        )
        expect(
            set(timeline_repair_request).issubset(
                {
                    "user_message",
                    "current_time",
                    "living_context_situation_candidates",
                    "validation_issues",
                    "repair_hints",
                    "previous_candidate",
                }
            )
            and "required_json_shape" not in timeline_repair_request
            and "primary_understanding" not in timeline_repair_request
            and isinstance(timeline_repair_request.get("repair_hints"), list)
            and timeline_repair_request["repair_hints"][0].get("field") == "timeline"
            and timeline_repair_request["repair_hints"][0].get("max_items") == 12
            and (
                not isinstance(timeline_previous.get("timeline"), list)
                or len(timeline_previous.get("timeline", [])) <= 12
            ),
            f"timeline {shape_name} repair request is bounded and schema-shaped",
        )

    bad_timeline_row = {
        "statement": "这条坏时间线不应保留",
        "source_quote": {"text": "不在当前用户消息", "start": 0, "end": 8},
        "material": True,
    }
    good_timeline_row = {
        "statement": "保留这条用户报告的时间线",
        "occurred_at": None,
        "material": True,
    }
    mixed_timeline_candidate = copy.deepcopy(candidate_payload(category="travel"))
    mixed_timeline_candidate["timeline"] = [bad_timeline_row, good_timeline_row]
    mixed_timeline_result = extract_living_context_candidate(
        FakeModelClient(
            [
                {
                    "status": "model_assisted",
                    "living_context_candidate": mixed_timeline_candidate,
                }
            ]
        ),
        text=text,
    )
    mixed_timeline_metrics_dump = json.dumps(
        mixed_timeline_result.metrics,
        ensure_ascii=False,
    )
    expect(
        mixed_timeline_result.candidate is not None
        and [row.statement for row in mixed_timeline_result.candidate.timeline]
        == [good_timeline_row["statement"]]
        and mixed_timeline_result.metrics.get("dropped_timeline_count") == 1
        and mixed_timeline_result.metrics.get("normalization_status") == "normalized"
        and "这条坏时间线不应保留" not in mixed_timeline_metrics_dump
        and "不在当前用户消息" not in mixed_timeline_metrics_dump,
        "one bad timeline row is quarantined while one strict row remains",
    )

    all_bad_timeline_candidate = copy.deepcopy(candidate_payload(category="travel"))
    all_bad_timeline_candidate["timeline"] = [
        bad_timeline_row,
        {"statement": 42, "authority": "drop-this-row"},
    ]
    all_bad_timeline_result = extract_living_context_candidate(
        FakeModelClient(
            [
                {
                    "status": "model_assisted",
                    "living_context_candidate": all_bad_timeline_candidate,
                }
            ]
        ),
        text=text,
    )
    expect(
        all_bad_timeline_result.candidate is not None
        and all_bad_timeline_result.candidate.timeline == []
        and all_bad_timeline_result.metrics.get("dropped_timeline_count") == 2
        and "drop-this-row"
        not in json.dumps(all_bad_timeline_result.metrics, ensure_ascii=False),
        "all invalid timeline rows quarantine to an empty optional projection",
    )

    bad_known_candidate = copy.deepcopy(candidate_payload(category="travel"))
    bad_known_candidate["timeline"] = [bad_timeline_row]
    bad_known_candidate["known"] = [{"statement": 42}]
    bad_known_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": bad_known_candidate,
            },
            {
                "status": "model_assisted",
                "living_context_candidate": bad_known_candidate,
            },
        ]
    )
    bad_known_result = extract_living_context_candidate(bad_known_client, text=text)
    expect(
        bad_known_result.candidate is None
        and any("known.0.statement:string_type" in issue for issue in bad_known_result.issues),
        "bad required known evidence still rejects the candidate after timeline quarantine",
    )

    bad_binding_candidate = copy.deepcopy(candidate_payload(category="travel"))
    bad_binding_candidate.update(
        {
            "disposition": "update",
            "situation_token": "sit_not_in_current_catalog",
            "situation_revision": 7,
            "catalog_token": "cat_not_in_current_catalog",
            "timeline": [bad_timeline_row],
        }
    )
    bad_binding_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": bad_binding_candidate,
            },
            {
                "status": "model_assisted",
                "living_context_candidate": bad_binding_candidate,
            },
        ]
    )
    bad_binding_result = extract_living_context_candidate(
        bad_binding_client,
        text=text,
        catalog=[],
    )
    expect(
        bad_binding_result.candidate is None
        and any("binding:not_in_catalog" in issue for issue in bad_binding_result.issues),
        "bad Situation binding still rejects the candidate after timeline quarantine",
    )

    timeline_still_invalid = copy.deepcopy(candidate_payload(category="travel"))
    timeline_still_invalid["timeline"] = "still not a list"
    timeline_reject_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": timeline_still_invalid,
            },
            {
                "status": "model_assisted",
                "living_context_candidate": timeline_still_invalid,
            },
        ]
    )
    timeline_rejected = extract_living_context_candidate(
        timeline_reject_client,
        text=text,
    )
    expect(
        timeline_rejected.candidate is None
        and timeline_rejected.metrics.get("status") == "rejected"
        and timeline_rejected.metrics.get("repair_attempted") is True
        and timeline_reject_client.call_count == 2,
        "timeline remains fail-closed after one invalid candidate-only repair",
    )

    timeline_primary_invalid = copy.deepcopy(candidate_payload(category="travel"))
    timeline_primary_invalid["timeline"] = {"statement": "primary malformed timeline"}
    timeline_understanding_reasoning = FakeReasoning(
        [
            model_payload(timeline_primary_invalid, text=text),
            {
                "status": "model_assisted",
                "living_context_candidate": timeline_repaired,
            },
        ]
    )
    timeline_understanding = UnderstandingCore(timeline_understanding_reasoning).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": []},
    )
    expect(
        timeline_understanding.living_context_candidate is not None
        and timeline_understanding.living_context_candidate.timeline
        and timeline_understanding_reasoning.client.purposes
        == ["turn_understanding", "living_context_candidate_extraction"],
        "invalid primary timeline reaches the shared candidate-only repair path",
    )

    missing_schema_candidate = copy.deepcopy(candidate_payload(category="travel"))
    missing_schema_candidate.pop("schema_version")
    missing_schema_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": missing_schema_candidate,
            }
        ]
    )
    missing_schema_result = extract_living_context_candidate(
        missing_schema_client,
        text=text,
    )
    strict_missing_schema, strict_missing_issues, _strict_missing_report = (
        parse_living_context_candidate_detailed(missing_schema_candidate)
    )
    expect(
        strict_missing_schema is None
        and any("schema_version:missing" in issue for issue in strict_missing_issues),
        "direct contract remains strict when schema metadata is absent",
    )
    expect(
        missing_schema_result.candidate is not None
        and missing_schema_result.candidate.disposition == "create"
        and missing_schema_result.metrics.get("schema_version_normalized") is True
        and missing_schema_result.metrics.get("normalized") is True
        and missing_schema_result.metrics.get("normalization_status") == "normalized"
        and "schema_version:normalized"
        in missing_schema_result.metrics.get("repaired_fields", [])
        and missing_schema_result.metrics.get("normalized_fields") == ["schema_version"],
        "missing candidate schema metadata receives only the fixed V1 normalization",
    )

    missing_schema_root = {
        **missing_schema_candidate,
        "status": "model_assisted",
    }
    missing_schema_root_result = extract_living_context_candidate(
        FakeModelClient([missing_schema_root]),
        text=text,
    )
    expect(
        missing_schema_root_result.candidate is not None
        and missing_schema_root_result.metrics.get("schema_version_normalized") is True,
        "root candidate with missing schema metadata uses the same fixed normalization",
    )

    wrong_schema_candidate = copy.deepcopy(candidate_payload(category="travel"))
    wrong_schema_candidate["schema_version"] = "veyra.living_context_candidate.v0"
    wrong_schema_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": wrong_schema_candidate,
            },
            {
                "status": "model_assisted",
                "living_context_candidate": wrong_schema_candidate,
            },
        ]
    )
    wrong_schema_result = extract_living_context_candidate(
        wrong_schema_client,
        text=text,
    )
    expect(
        wrong_schema_result.candidate is None
        and wrong_schema_result.metrics.get("schema_version_normalized") is not True
        and wrong_schema_result.metrics.get("repair_attempted") is True,
        "explicitly wrong candidate schema metadata remains rejected",
    )

    missing_semantic_candidate = copy.deepcopy(missing_schema_candidate)
    missing_semantic_candidate.pop("create_subject")
    missing_semantic_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": missing_semantic_candidate,
            },
            {
                "status": "model_assisted",
                "living_context_candidate": missing_semantic_candidate,
            },
        ]
    )
    missing_semantic_result = extract_living_context_candidate(
        missing_semantic_client,
        text=text,
    )
    expect(
        missing_semantic_result.candidate is None
        and any("create_subject_missing" in issue for issue in missing_semantic_result.issues),
        "missing semantic candidate fields remain rejected after metadata normalization",
    )

    missing_binding_candidate = copy.deepcopy(missing_schema_candidate)
    missing_binding_candidate.update(
        {
            "disposition": "update",
            "summary": "existing Situation update",
        }
    )
    missing_binding_candidate.pop("situation_token", None)
    missing_binding_candidate.pop("situation_revision", None)
    missing_binding_candidate.pop("catalog_token", None)
    missing_binding_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": missing_binding_candidate,
            },
            {
                "status": "model_assisted",
                "living_context_candidate": missing_binding_candidate,
            },
        ]
    )
    missing_binding_result = extract_living_context_candidate(
        missing_binding_client,
        text=text,
        catalog=[
            {
                "situation_token": "sit_schema_boundary",
                "situation_revision": 2,
                "catalog_token": "cat_schema_boundary",
            }
        ],
    )
    expect(
        missing_binding_result.candidate is None
        and any("update_token_missing" in issue for issue in missing_binding_result.issues),
        "missing Situation bindings remain rejected after metadata normalization",
    )

    quiet_missing_schema = {
        "disposition": "quiet",
        "source": "model",
    }
    quiet_missing_schema_result = extract_living_context_candidate(
        FakeModelClient(
            [
                {
                    "status": "model_assisted",
                    "living_context_candidate": quiet_missing_schema,
                },
                {
                    "status": "model_assisted",
                    "living_context_candidate": quiet_missing_schema,
                },
            ]
        ),
        text=text,
    )
    expect(
        quiet_missing_schema_result.candidate is None
        and quiet_missing_schema_result.metrics.get("schema_version_normalized") is not True,
        "quiet marker without schema metadata is not promoted into a candidate",
    )

    root_transport_payload = {
        **candidate_payload(category="travel"),
        "disposition": "create",
        "status": "model_assisted",
        "model_status": "ok",
        "duration_ms": 12,
        "_model": {"purpose": "living_context_candidate_extraction"},
    }
    root_extraction = extract_living_context_candidate(
        FakeModelClient([root_transport_payload]),
        text=text,
    )
    expect(
        root_extraction.candidate is not None
        and root_extraction.metrics.get("status") in {"accepted", "repaired"},
        "candidate extractor strips only known transport keys from an exact root candidate",
    )

    random_root = extract_living_context_candidate(
        FakeModelClient(
            [
                {
                    "status": "model_assisted",
                    "schema_version": "veyra.living_context_candidate.v1",
                    "disposition": "not_a_disposition",
                    "payload": candidate_payload(category="travel"),
                }
            ]
        ),
        text=text,
    )
    expect(
        random_root.candidate is None
        and random_root.metrics.get("status") == "rejected"
        and random_root.issues == ["living_context_candidate:extractor:candidate_missing"],
        "random root objects remain rejected instead of broad-scanned",
    )

    exact_catalog = {
        "situation_token": "sit_extractor_demo",
        "situation_revision": 4,
        "catalog_token": "cat_extractor_demo",
    }
    invalid_source_candidate = copy.deepcopy(candidate_payload(category="travel"))
    invalid_source_candidate["needs"][0]["allowed_source_classes"] = ["unsupported_provider"]
    update_candidate = {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": "update",
        "situation_token": exact_catalog["situation_token"],
        "situation_revision": exact_catalog["situation_revision"],
        "catalog_token": exact_catalog["catalog_token"],
        "summary": "现有事项出现新的进展",
        "source": "model",
    }
    invalid_extractor_response = {
        "status": "model_assisted",
        "living_context_candidate": invalid_source_candidate,
    }
    repair_valid_client = FakeModelClient(
        [
            invalid_extractor_response,
            {
                "status": "model_assisted",
                "living_context_candidate": candidate_payload(category="travel"),
            },
        ]
    )
    repair_valid = extract_living_context_candidate(repair_valid_client, text=text)
    expect(
        repair_valid.candidate is not None
        and repair_valid.metrics.get("status") == "repaired"
        and repair_valid_client.call_count == 2,
        "invalid candidate receives exactly one candidate-only repair and becomes valid",
    )

    repair_invalid_client = FakeModelClient(
        [invalid_extractor_response, invalid_extractor_response]
    )
    repair_invalid = extract_living_context_candidate(repair_invalid_client, text=text)
    expect(
        repair_invalid.candidate is None
        and repair_invalid.metrics.get("status") == "rejected"
        and repair_invalid.metrics.get("repair_attempted") is True
        and repair_invalid_client.call_count == 2,
        "invalid candidate repair remains rejected after the single bounded retry",
    )

    forbidden_response = {
        "status": "model_assisted",
        "living_context_candidate": {
            **candidate_payload(category="travel"),
            "route": "agent",
        },
    }
    forbidden_client = FakeModelClient([forbidden_response])
    forbidden = extract_living_context_candidate(forbidden_client, text=text)
    expect(
        forbidden.candidate is None
        and forbidden.metrics.get("issue_codes") == ["living_context_candidate:extractor:forbidden_field"]
        and forbidden_client.call_count == 1,
        "forbidden authority fields reject immediately without repair",
    )

    malicious_boundary_candidate = copy.deepcopy(candidate_payload(category="travel"))
    malicious_boundary_candidate[malicious_extra_key] = "repair-secret-token"
    malicious_boundary_response = {
        "status": "model_assisted",
        "living_context_candidate": malicious_boundary_candidate,
    }
    malicious_boundary_client = FakeModelClient(
        [malicious_boundary_response, malicious_boundary_response]
    )
    malicious_boundary_result = extract_living_context_candidate(
        malicious_boundary_client,
        text=text,
    )
    malicious_boundary_metrics_dump = json.dumps(
        malicious_boundary_result.metrics,
        ensure_ascii=False,
    )
    malicious_boundary_prompt_dump = json.dumps(
        malicious_boundary_client.users[1],
        ensure_ascii=False,
    )
    expect(
        malicious_boundary_result.candidate is None
        and malicious_boundary_result.issues
        == ["living_context_candidate:extra_field:extra_forbidden"]
        and malicious_boundary_result.metrics.get("issue_codes")
        == ["living_context_candidate:extra_field:extra_forbidden"]
        and malicious_boundary_client.call_count == 2
        and malicious_extra_key not in malicious_boundary_metrics_dump
        and "SECRET_TOKEN" not in malicious_boundary_metrics_dump
        and "repair-secret-token" not in malicious_boundary_metrics_dump
        and malicious_extra_key not in malicious_boundary_prompt_dump
        and "SECRET_TOKEN" not in malicious_boundary_prompt_dump
        and "repair-secret-token" not in malicious_boundary_prompt_dump,
        "malicious extra keys stay sanitized across initial parse, repair, metrics, and prompt",
    )

    rebound_candidate = {
        **update_candidate,
        "situation_token": "sit_not_in_catalog",
    }
    rebound_response = {
        "status": "model_assisted",
        "living_context_candidate": rebound_candidate,
    }
    rebound_client = FakeModelClient([rebound_response, rebound_response])
    rebound = extract_living_context_candidate(
        rebound_client,
        text=text,
        catalog=[exact_catalog],
    )
    expect(
        rebound.candidate is None
        and rebound.metrics.get("issue_codes")
        == ["living_context_candidate:binding:not_in_catalog"]
        and rebound_client.call_count == 2,
        "token rebinding outside the current catalog remains rejected",
    )
    second_catalog = {
        "situation_token": "sit_extractor_other",
        "situation_revision": 2,
        "catalog_token": "cat_extractor_other",
        "summary": "另一个事项",
    }
    third_catalog = {
        "situation_token": "sit_extractor_third",
        "situation_revision": 7,
        "catalog_token": "cat_extractor_third",
        "summary": "第三个事项",
    }
    unknown_binding_candidate = {
        **update_candidate,
        "situation_token": "sit_missing_from_catalog",
        "situation_revision": 99,
        "catalog_token": "cat_missing_from_catalog",
        "answered_need_tokens": ["need_stale_reference"],
        "answered_need_bindings": [
            {"need_token": "need_stale_reference", "generation": 1}
        ],
    }
    unknown_binding_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": unknown_binding_candidate,
            },
            {
                "status": "model_assisted",
                "living_context_candidate": {
                    **update_candidate,
                    "situation_token": exact_catalog["situation_token"],
                    "situation_revision": exact_catalog["situation_revision"],
                    "catalog_token": exact_catalog["catalog_token"],
                },
            },
        ]
    )
    unknown_binding_repaired = extract_living_context_candidate(
        unknown_binding_client,
        text=text,
        catalog=[exact_catalog, second_catalog, third_catalog],
    )
    unknown_repair_request = unknown_binding_client.users[1]
    unknown_repair_catalog = unknown_repair_request.get(
        "living_context_situation_candidates",
        [],
    )
    unknown_previous = unknown_repair_request.get("previous_candidate", {})
    expect(
        unknown_binding_repaired.candidate is not None
        and unknown_binding_repaired.candidate.situation_token == exact_catalog["situation_token"]
        and len(unknown_repair_catalog) == 3
        and {row.get("situation_token") for row in unknown_repair_catalog}
        == {
            exact_catalog["situation_token"],
            second_catalog["situation_token"],
            third_catalog["situation_token"],
        }
        and all(field not in unknown_previous for field in (
            "situation_token",
            "situation_revision",
            "catalog_token",
            "answered_need_tokens",
            "answered_need_bindings",
        )),
        "unknown binding repair receives the full bounded catalog without opaque bindings",
    )
    ambiguous_first = {
        "situation_token": "sit_ambiguous_catalog",
        "situation_revision": 1,
        "catalog_token": "cat_ambiguous_one",
    }
    ambiguous_second = {
        "situation_token": "sit_ambiguous_catalog",
        "situation_revision": 2,
        "catalog_token": "cat_ambiguous_two",
    }
    ambiguous_candidate = {
        **update_candidate,
        "situation_token": "sit_ambiguous_catalog",
        "situation_revision": 99,
        "catalog_token": "cat_ambiguous_unknown",
    }
    ambiguous_response = {
        "status": "model_assisted",
        "living_context_candidate": ambiguous_candidate,
    }
    ambiguous_client = FakeModelClient([ambiguous_response, ambiguous_response])
    ambiguous_result = extract_living_context_candidate(
        ambiguous_client,
        text=text,
        catalog=[ambiguous_first, ambiguous_second, third_catalog],
    )
    ambiguous_repair_request = ambiguous_client.users[1]
    ambiguous_previous = ambiguous_repair_request.get("previous_candidate", {})
    expect(
        ambiguous_result.candidate is None
        and ambiguous_result.issues
        == ["living_context_candidate:binding:not_in_catalog"]
        and len(ambiguous_repair_request.get("living_context_situation_candidates", [])) == 3
        and all(field not in ambiguous_previous for field in (
            "situation_token",
            "situation_revision",
            "catalog_token",
            "answered_need_tokens",
            "answered_need_bindings",
        )),
        "ambiguous catalog token remains fail-closed and does not narrow repair context",
    )
    stale_revision_candidate = {
        **update_candidate,
        "situation_revision": 99,
        "catalog_token": "cat_wrong_for_selected_situation",
    }
    narrowed_repair_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": stale_revision_candidate,
            },
            {
                "status": "model_assisted",
                "living_context_candidate": update_candidate,
            },
        ]
    )
    narrowed_repair = extract_living_context_candidate(
        narrowed_repair_client,
        text=text,
        catalog=[exact_catalog, second_catalog, third_catalog],
    )
    repair_catalog = narrowed_repair_client.users[1].get(
        "living_context_situation_candidates",
        [],
    )
    narrowed_previous = narrowed_repair_client.users[1].get("previous_candidate", {})
    expect(
        narrowed_repair.candidate is not None
        and narrowed_repair.candidate.situation_revision == exact_catalog["situation_revision"]
        and isinstance(repair_catalog, list)
        and len(repair_catalog) == 1
        and repair_catalog[0].get("situation_token") == exact_catalog["situation_token"]
        and narrowed_previous.get("situation_token") == exact_catalog["situation_token"]
        and all(field not in narrowed_previous for field in (
            "situation_revision",
            "catalog_token",
            "answered_need_tokens",
            "answered_need_bindings",
        )),
        "repair narrows a unique Situation token and drops unproven binding fields",
    )

    catalog_token_candidate = {
        **update_candidate,
        "situation_token": "sit_missing_for_catalog_selector",
        "situation_revision": 99,
        "catalog_token": exact_catalog["catalog_token"],
    }
    catalog_token_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": catalog_token_candidate,
            },
            {
                "status": "model_assisted",
                "living_context_candidate": update_candidate,
            },
        ]
    )
    catalog_token_repaired = extract_living_context_candidate(
        catalog_token_client,
        text=text,
        catalog=[exact_catalog, second_catalog, third_catalog],
    )
    catalog_token_request = catalog_token_client.users[1]
    catalog_token_repair_catalog = catalog_token_request.get(
        "living_context_situation_candidates",
        [],
    )
    catalog_token_previous = catalog_token_request.get("previous_candidate", {})
    expect(
        catalog_token_repaired.candidate is not None
        and catalog_token_repaired.candidate.situation_token == exact_catalog["situation_token"]
        and len(catalog_token_repair_catalog) == 1
        and catalog_token_repair_catalog[0].get("catalog_token") == exact_catalog["catalog_token"]
        and catalog_token_previous.get("catalog_token") == exact_catalog["catalog_token"]
        and all(field not in catalog_token_previous for field in (
            "situation_token",
            "situation_revision",
            "answered_need_tokens",
            "answered_need_bindings",
        )),
        "repair falls back to a unique catalog token and drops the other binding fields",
    )

    catalog_token_illegal_client = FakeModelClient(
        [
            {
                "status": "model_assisted",
                "living_context_candidate": catalog_token_candidate,
            },
            {
                "status": "model_assisted",
                "living_context_candidate": catalog_token_candidate,
            },
        ]
    )
    catalog_token_illegal = extract_living_context_candidate(
        catalog_token_illegal_client,
        text=text,
        catalog=[exact_catalog, second_catalog, third_catalog],
    )
    expect(
        catalog_token_illegal.candidate is None
        and catalog_token_illegal.issues
        == ["living_context_candidate:binding:not_in_catalog"]
        and catalog_token_illegal.metrics.get("repair_attempted") is True
        and catalog_token_illegal_client.call_count == 2,
        "a second illegal binding remains fail-closed after the bounded repair",
    )
    invalid_primary = model_payload(
        invalid_source_candidate,
        text=text,
        feedback=feedback,
    )
    canonical_extractor_reasoning = FakeReasoning(
        [
            invalid_primary,
            {
                "status": "model_assisted",
                "living_context_candidate": update_candidate,
            },
        ]
    )
    canonical_extracted = UnderstandingCore(canonical_extractor_reasoning).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": [exact_catalog]},
    )
    expect(
        canonical_extracted.living_context_candidate is not None
        and canonical_extracted.living_context_candidate.disposition == "update"
        and canonical_extracted.living_context_candidate.situation_token == exact_catalog["situation_token"],
        "rejected primary candidate recovers through an exact canonical update extractor",
    )
    expect(
        canonical_extractor_reasoning.client.purposes == [
            "turn_understanding",
            "living_context_candidate_extraction",
        ],
        "rejected candidate uses one extractor and skips the full response repair",
    )

    failed_extractor_reasoning = FakeReasoning(
        [invalid_primary, invalid_extractor_response, invalid_extractor_response]
    )
    failed_extraction = UnderstandingCore(failed_extractor_reasoning).build(
        text=text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": [exact_catalog]},
    )
    expect(
        failed_extraction.living_context_candidate is None
        and failed_extraction.living_reaction_feedback is not None
        and failed_extraction.living_context_candidate_metrics.get("status") == "rejected"
        and failed_extraction.living_context_candidate_metrics.get("issue_codes")
        == ["living_context_candidate:root:unsupported_source_class"],
        "invalid extractor remains fail-closed while independent feedback survives",
    )
    expect(
        failed_extractor_reasoning.client.purposes == [
            "turn_understanding",
            "living_context_candidate_extraction",
            "living_context_candidate_extraction_repair",
        ]
        and failed_extractor_reasoning.client.call_count == 3,
        "invalid extractor does not fall through to a second full repair",
    )
    expect(
        repaired.living_reaction_feedback is not None
        and repaired.living_reaction_feedback.source_quote.text == text,
        "feedback quote remains source-bound",
    )
    aliased_feedback, feedback_issues, feedback_report = parse_living_reaction_feedback_detailed(
        {**feedback, "label": "有用"},
        source_text=text,
    )
    expect(
        aliased_feedback is not None
        and not feedback_issues
        and feedback_report["status"] == "repaired"
        and aliased_feedback.label == "useful",
        "known feedback label alias repairs without changing token or quote",
    )

    rejected_payload = model_payload(candidate_payload(category="未知类别"), text=text, feedback=feedback)
    partial = build_understanding([rejected_payload, rejected_payload], text)
    expect(partial.living_context_candidate is None, "unknown enum is rejected without keyword fallback")
    expect(partial.living_reaction_feedback is not None, "valid feedback survives candidate rejection and repair")
    expect(
        partial.living_context_candidate_metrics.get("status") == "rejected"
        and partial.living_reaction_feedback_metrics.get("status") == "accepted"
        and partial.model_boundary_metrics.get("repair_attempted") is True,
        "partial boundary metrics record rejection and bounded repair attempt",
    )

    # Feedback-only recovery is a separate, opt-in model seam.  The catalog
    # below deliberately has three distinct current reactions so the tests
    # prove row selection and feedback labels without any scenario vocabulary.
    reaction_catalog = [
        {
            "owner_id": "feedback-owner",
            "session_id": "feedback-session",
            "situation_token": "sit_feedback_one",
            "situation_selector": "sitref_1111111111111111",
            "title": "第一个事项",
            "reaction": {
                "reaction_token": "rxn_11111111111111111111111111111111",
                "reaction_revision": 2,
                "disposition": "suggest",
            },
        },
        {
            "owner_id": "feedback-owner",
            "session_id": "feedback-session",
            "situation_token": "sit_feedback_two",
            "situation_selector": "sitref_2222222222222222",
            "title": "第二个事项",
            "reaction": {
                "reaction_token": "rxn_22222222222222222222222222222222",
                "reaction_revision": 4,
                "disposition": "wait",
            },
        },
        {
            "owner_id": "feedback-owner",
            "session_id": "feedback-session",
            "situation_token": "sit_feedback_three",
            "situation_selector": "sitref_3333333333333333",
            "title": "第三个事项",
            "reaction": {
                "reaction_token": "rxn_33333333333333333333333333333333",
                "reaction_revision": 5,
                "disposition": "ask",
            },
        },
    ]
    for row in reaction_catalog:
        row["situation_selector"] = situation_catalog_selector(
            row["owner_id"], row["session_id"], row["situation_token"]
        )
    ignore_text = "这个提醒不用管"
    ignore_feedback = {
        "schema_version": "veyra.living_reaction_feedback.v1",
        "reaction_token": reaction_catalog[0]["reaction"]["reaction_token"],
        "label": "ignore",
        "source_quote": {"text": ignore_text, "start": 0, "end": len(ignore_text)},
    }
    ignore_extracted = extract_living_reaction_feedback(
        FakeModelClient([{"living_reaction_feedback": ignore_feedback}]),
        text=ignore_text,
        catalog=reaction_catalog,
    )
    expect(
        ignore_extracted.feedback is not None
        and ignore_extracted.feedback.label == "ignore"
        and ignore_extracted.feedback.reaction_token == reaction_catalog[0]["reaction"]["reaction_token"],
        "feedback seam binds explicit ignore to one current catalog row",
    )
    remind_text = "以后提前三天提醒我"
    remind_feedback = {
        "schema_version": "veyra.living_reaction_feedback.v1",
        "reaction_token": reaction_catalog[2]["reaction"]["reaction_token"],
        "label": "remind_before",
        "remind_before_seconds": 259200,
        "source_quote": {"text": remind_text, "start": 0, "end": len(remind_text)},
    }
    remind_extracted = extract_living_reaction_feedback(
        FakeModelClient([{"living_reaction_feedback": remind_feedback}]),
        text=remind_text,
        catalog=reaction_catalog,
    )
    expect(
        remind_extracted.feedback is not None
        and remind_extracted.feedback.label == "remind_before"
        and remind_extracted.feedback.remind_before_seconds == 259200
        and remind_extracted.feedback.reaction_token == reaction_catalog[2]["reaction"]["reaction_token"],
        "feedback seam preserves bounded remind_before timing",
    )
    ordinary_text = "补充一个普通进展"
    ordinary_reasoning = FakeReasoning(
        [
            model_payload(candidate_payload(category="travel"), text=ordinary_text),
            {"living_reaction_feedback": None},
        ]
    )
    ordinary_understanding = UnderstandingCore(ordinary_reasoning).build(
        text=ordinary_text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": reaction_catalog},
    )
    expect(
        ordinary_understanding.living_context_candidate is not None
        and ordinary_understanding.living_reaction_feedback is None
        and ordinary_understanding.living_reaction_feedback_metrics.get("status") == "absent"
        and ordinary_reasoning.client.purposes == [
            "turn_understanding",
            "living_reaction_feedback_extraction",
        ],
        "ordinary update returns explicit feedback absence through one bounded call",
    )
    stale_feedback = dict(ignore_feedback)
    stale_feedback["reaction_token"] = "rxn_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    stale_extracted = extract_living_reaction_feedback(
        FakeModelClient([{"living_reaction_feedback": stale_feedback}]),
        text=ignore_text,
        catalog=reaction_catalog,
    )
    expect(
        stale_extracted.feedback is None
        and stale_extracted.metrics.get("status") == "rejected"
        and stale_extracted.issues == ["living_reaction_feedback:extractor:token_not_unique"],
        "stale reaction tokens remain fail-closed",
    )
    ambiguous_catalog = [
        {"situation_selector": "sitref_aaaaaaaaaaaaaaaa", "reaction": dict(reaction_catalog[0]["reaction"])},
        {"situation_selector": "sitref_aaaaaaaaaaaaaaaa", "reaction": dict(reaction_catalog[1]["reaction"])},
    ]
    ambiguous_feedback = dict(ignore_feedback)
    ambiguous_feedback.pop("reaction_token")
    ambiguous_feedback["situation_selector"] = "sitref_aaaaaaaaaaaaaaaa"
    ambiguous_extracted = extract_living_reaction_feedback(
        FakeModelClient([{"living_reaction_feedback": ambiguous_feedback}]),
        text=ignore_text,
        catalog=ambiguous_catalog,
    )
    expect(
        ambiguous_extracted.feedback is None
        and ambiguous_extracted.issues == ["living_reaction_feedback:extractor:selector_not_unique"],
        "ambiguous row selectors remain fail-closed",
    )
    outside_feedback = dict(ignore_feedback)
    outside_feedback["reaction_token"] = "rxn_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    outside_extracted = extract_living_reaction_feedback(
        FakeModelClient([{"living_reaction_feedback": outside_feedback}]),
        text=ignore_text,
        catalog=reaction_catalog,
    )
    expect(
        outside_extracted.feedback is None
        and outside_extracted.issues == ["living_reaction_feedback:extractor:token_not_unique"],
        "token outside the current catalog remains rejected",
    )
    bad_quote_feedback = dict(ignore_feedback)
    bad_quote_feedback["source_quote"] = {"text": "不在用户消息", "start": 0, "end": 5}
    bad_quote_extracted = extract_living_reaction_feedback(
        FakeModelClient([{"living_reaction_feedback": bad_quote_feedback}]),
        text=ignore_text,
        catalog=reaction_catalog,
    )
    expect(
        bad_quote_extracted.feedback is None
        and any("source_quote:not_source_bound" in issue for issue in bad_quote_extracted.issues),
        "feedback source quotes remain bound to the current user message",
    )
    same_turn_feedback = model_payload(
        candidate_payload(category="travel"),
        text=ordinary_text,
        feedback={
            **ignore_feedback,
            "source_quote": {"text": ordinary_text, "start": 0, "end": len(ordinary_text)},
        },
    )
    same_turn_reasoning = FakeReasoning([same_turn_feedback])
    same_turn_understanding = UnderstandingCore(same_turn_reasoning).build(
        text=ordinary_text,
        attention_focus=[],
        turn_context={"living_context_situation_candidates": reaction_catalog},
    )
    expect(
        same_turn_understanding.living_context_candidate is not None
        and same_turn_understanding.living_reaction_feedback is not None
        and same_turn_reasoning.client.purposes == ["turn_understanding"],
        "candidate and feedback in one turn remain independent without a duplicate call",
    )

    invalid, issues, report = parse_living_context_candidate_detailed(
        {**candidate_payload(), "category": "travel-ish"}
    )
    expect(invalid is None and issues and report["status"] == "rejected", "unknown enum remains fail-closed")
    expect(
        all("input_value" not in issue and "secret" not in issue.lower() for issue in issues),
        "validation issues contain codes only, never raw model values",
    )
    unsupported_source = copy.deepcopy(candidate_payload(category="travel"))
    unsupported_source["needs"][0]["allowed_source_classes"] = ["untrusted_provider_class"]
    invalid_source, source_issues, source_report = parse_living_context_candidate_detailed(
        unsupported_source,
    )
    expect(
        invalid_source is None
        and source_report["status"] == "rejected"
        and source_issues == ["living_context_candidate:root:unsupported_source_class"]
        and all("untrusted_provider_class" not in issue for issue in source_issues),
        "unsupported source class stays fail-closed with a safe validator code",
    )

    bad_quote = candidate_payload()
    bad_quote["known"] = [
        {
            "statement": "用户报告下周去上海出差",
            "source_quote": {"text": "不在原文", "start": 0, "end": 3},
            "epistemic_status": "报告",
        }
    ]
    invalid_quote, quote_issues, quote_report = parse_living_context_candidate_detailed(
        bad_quote,
        source_text=text,
    )
    expect(
        invalid_quote is not None
        and not quote_issues
        and invalid_quote.known == []
        and quote_report.get("dropped_known_count") == 1
        and "known:quarantined" in quote_report.get("repaired_fields", []),
        "optional Known quote quarantine preserves the candidate without rewriting it",
    )
    bad_root_quote = candidate_payload()
    bad_root_quote["source_quote"] = {
        "text": "不在原文",
        "start": 0,
        "end": 3,
    }
    invalid_root_quote, root_quote_issues, _root_quote_report = (
        parse_living_context_candidate_detailed(
            bad_root_quote,
            source_text=text,
        )
    )
    expect(
        invalid_root_quote is None
        and any("source_quote:not_source_bound" in issue for issue in root_quote_issues),
        "candidate root quote remains strict after Known row quarantine",
    )
    print("INFO deterministic boundary smoke; provider usefulness requires a separate live acceptance run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
