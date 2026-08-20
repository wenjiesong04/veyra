#!/usr/bin/env python3
"""Exercise the real UnderstandingCore parser with a synonym-shaped model turn."""

from __future__ import annotations

import copy
import json
import sys
from tempfile import TemporaryDirectory
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.turn_context_builder import TurnContextBuilder  # noqa: E402
from core.understanding_core import UnderstandingCore  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS {label}")


class FakeModelClient:
    """Deterministic transport seam; UnderstandingCore remains production code."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def complete_json(self, *, purpose: str, system: str, user: str) -> dict[str, object]:
        return self.payload


class FakeReasoning:
    def __init__(self, payload: dict[str, object]) -> None:
        self.client = FakeModelClient(payload)

    def is_enabled(self) -> bool:
        return True

    def _trace(self, *args: object, **kwargs: object) -> None:
        return None


def main() -> int:
    text = "最近在准备求职面谈"
    quote = {"text": text, "start": 0, "end": len(text)}
    payload: dict[str, object] = {
        "status": "model_assisted",
        "source": "model",
        "situation_assessment": {
            "intent": "conversation",
            "task_type": "chat",
            "task_summary": "近期准备面试",
            "explicit_request": text,
            "hidden_need": "持续维护面试准备上下文",
            "user_goal": "准备好面试",
            "suggested_mode": "direct_answer",
            "confidence": 0.86,
            "living_context_candidate": {
                "schema_version": "veyra.living_context_candidate.v1",
                "disposition": "create",
                "create_subject": "面试准备",
                "category": "work",
                "label": "面试准备",
                "title": "面试准备",
                "summary": "近期准备面试",
                "goal": "准备好面试",
                "lifecycle": "active",
                "known": [
                    {"statement": "用户正在准备面试", "epistemic_status": "reported"}
                ],
                "unknown": ["面试时间和准备重点"],
                "assumptions": [],
                "timeline": [],
                "material_change": "",
                "next_step": "确认面试时间",
                "next_step_epistemic_status": "inferred",
                "needs": [
                    {
                        "blocked_judgment": "面试时间和准备重点",
                        "evidence_kind": "user",
                        "why_now": "它会改变下一步准备重点",
                        "urgency": 0.6,
                        "allowed_source_classes": ["user"],
                        "fallback_reaction": "ask",
                        "question": "面试大约什么时候、最想先准备哪一部分？",
                    }
                ],
                "answered_need_tokens": [],
                "answered_need_bindings": [],
                "requested_reaction": "ask",
                "reopen": False,
                "reopen_reason": "",
                "source_quote": None,
                "assertion_mode": "inferred",
                "source": "model",
            },
        },
        "semantic_frame": {
            "schema_version": "veyra.semantic_frame.v1",
            "acts": [
                {
                    "act_id": "a1",
                    "kind": "statement",
                    "goal": "记录用户正在准备面试",
                    "operation": "report",
                    "target": {"type": "situation", "value": "面试准备", "attributes": {}},
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
        },
    }
    understanding = UnderstandingCore(FakeReasoning(payload)).build(
        text=text,
        attention_focus=["面试准备"],
        turn_context={"living_context_situation_candidates": []},
    )
    expect(understanding.source == "model", "real UnderstandingCore accepts model-shaped output")
    expect(understanding.living_context_candidate is not None, "candidate survives strict UnderstandingCore parsing")
    candidate = understanding.living_context_candidate
    expect(candidate is not None and candidate.create_subject == "面试准备", "synonym-shaped message maps to a typed Situation subject")
    expect(candidate is not None and candidate.needs[0].fallback_reaction == "ask", "InformationNeed remains typed after parsing")
    expect(understanding.semantic_frame is not None and understanding.semantic_frame.acts[0].source_quote.text == text, "semantic frame remains source-bound")

    # Production-shaped path: AwarenessLoop obtains the orchestrator catalog,
    # TurnContextBuilder projects it, then UnderstandingCore parses feedback
    # against the exact current server-issued reaction token.  The input row
    # deliberately contains forbidden raw fields to prove they do not cross
    # the model boundary.
    reaction_token = "rxn_" + ("a" * 32)
    need_token = "need_" + ("b" * 32)
    feedback_text = "刚才这个提醒很有用"
    feedback_payload = copy.deepcopy(payload)
    feedback_situation = feedback_payload["situation_assessment"]
    feedback_situation["living_context_candidate"] = {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": "quiet",
        "source": "model",
    }
    feedback_situation["living_reaction_feedback"] = {
        "schema_version": "veyra.living_reaction_feedback.v1",
        "reaction_token": reaction_token,
        "label": "useful",
        "remind_before_seconds": None,
        "source_quote": {"text": feedback_text, "start": 0, "end": len(feedback_text)},
    }
    feedback_payload["semantic_frame"]["acts"][0]["goal"] = "确认提醒有用"
    feedback_payload["semantic_frame"]["acts"][0]["source_quote"] = {
        "text": feedback_text,
        "start": 0,
        "end": len(feedback_text),
    }

    with TemporaryDirectory(prefix="veyra-understanding-catalog-") as tmp:
        builder = TurnContextBuilder(WorldStateStore(tmp))
        context = builder.build(
            user_message=feedback_text,
            attention_focus=[],
            living_context_situation_candidates=[
                {
                    "situation_token": "sit_sem_" + ("c" * 24),
                    "observation_revision": 4,
                    "catalog_token": "cat_" + ("d" * 32),
                    "owner_id": "catalog-user",
                    "session_id": "catalog-session",
                    "title": "上海出差",
                    "summary": "下周去上海出差",
                    "open_needs": [
                        {
                            "need_token": need_token,
                            "generation": 3,
                            "blocked_judgment": "具体出差日期",
                            "question": "出差是哪天？",
                            "status": "open",
                            "authority": {"execution": True},
                            "path": "/private/need",
                        }
                    ],
                    "reaction": {
                        "reaction_token": reaction_token,
                        "reaction_revision": 9,
                        "disposition": "ask",
                        "authority": {"execution": True, "delivery": True},
                        "path": "/private/reaction",
                    },
                }
            ],
        )
        catalog = context["living_context_situation_candidates"]
        expect(len(catalog) == 1, "TurnContextBuilder keeps one exact Situation catalog row")
        row = catalog[0]
        expect(
            row.get("reaction") == {
                "reaction_token": reaction_token,
                "reaction_revision": 9,
                "disposition": "ask",
            },
            "catalog keeps only current reaction token, revision, and disposition",
        )
        expect(
            row.get("open_needs") == [
                {
                    "need_token": need_token,
                    "generation": 3,
                    "blocked_judgment": "具体出差日期",
                    "question": "出差是哪天？",
                    "status": "open",
                }
            ],
            "catalog keeps open Need token/generation without raw fields",
        )
        serialized_catalog = json.dumps(catalog, ensure_ascii=False)
        expect("authority" not in serialized_catalog and "/private/" not in serialized_catalog, "catalog omits raw authority and paths")

        feedback_understanding = UnderstandingCore(FakeReasoning(feedback_payload)).build(
            text=feedback_text,
            attention_focus=[],
            turn_context=context,
        )
        expect(
            feedback_understanding.living_context_candidate is not None
            and feedback_understanding.living_context_candidate.disposition == "quiet",
            "feedback parse keeps Situation candidate independent",
        )
        expect(
            feedback_understanding.living_reaction_feedback is not None
            and feedback_understanding.living_reaction_feedback.reaction_token == reaction_token
            and feedback_understanding.living_reaction_feedback_metrics.get("status") == "accepted",
            "UnderstandingCore accepts the exact current reaction token from the builder catalog",
        )
        expect(
            feedback_understanding.living_reaction_feedback.source_quote.text == feedback_text,
            "feedback quote remains bound through the production-shaped context",
        )

    print("INFO no live model configured; this is automatic UnderstandingCore contract evidence, not usefulness proof")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
