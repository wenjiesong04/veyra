#!/usr/bin/env python3
"""Proves user-profile anchoring generalizes beyond hardcoded brands.

The pre-existing path only captured GSoC/Kotlin/Veyra/Agent治理系统 via brand-specific
regex. This smoke shows arbitrary projects, programs, and identities are now captured
generically from explicit self-statements, while staying isolated per user and not
mis-capturing questions.
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import Decision, EventSource, EventType, LoopResult, Route, VeyraEvent  # noqa: E402


def _event(user_id: str, session_id: str, text: str) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=user_id, session_id=session_id),
        payload={"text": text},
    )


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def profiles(store: WorldStateStore) -> dict:
    return store.read_json("user_world.json").get("profiles_by_user", {})


def authorized_profile_result(event: VeyraEvent) -> LoopResult:
    text = str(event.payload.get("text") or "")
    act_id = "profile-1"
    decision = Decision(
        route=Route.DIRECT_ANSWER,
        risk_level=RiskLevel.R0,
        reason="smoke-authorized profile statement",
        model_assist={
            "semantic_frame": {
                "schema_version": "veyra.semantic_frame.v1",
                "acts": [
                    {
                        "act_id": act_id,
                        "kind": "self_disclosure",
                        "goal": "record the explicit user fact",
                        "operation": "update_user_profile",
                        "target": {"type": "user_profile", "value": text, "attributes": {}},
                        "polarity": "positive",
                        "explicitness": "explicit",
                        "source_quote": {"text": text, "start": 0, "end": len(text)},
                        "speaker": event.source.user_id,
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
                "source": "smoke",
            },
            "semantic_policy": {
                "preferred_route": "direct_answer",
                "allowed_effects": ["profile.write"],
                "denied_effects": [],
                "authoritative_act_ids": [act_id],
                "requires_clarification": False,
            },
        },
    )
    return LoopResult(
        event_id=event.event_id,
        route=Route.DIRECT_ANSWER,
        status="success",
        response="",
        risk_level=RiskLevel.R0,
        artifacts={"decision": decision.to_dict()},
    )


def sync_profile(loop: AwarenessLoop, event: VeyraEvent) -> None:
    loop._sync_user_awareness_from_text(event, authorized_profile_result(event))


def main() -> None:
    with TemporaryDirectory(prefix="veyra-profile-generalization-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        loop = AwarenessLoop(store, RuntimeEntity(store))

        # 1. A project that was never hardcoded is captured generically.
        sync_profile(loop, _event("u1", "s1", "我在做一个电商小程序"))
        expect(profiles(store).get("u1", {}).get("current_project") == "电商小程序", "novel project captured", profiles(store))

        # 2. Different phrasing + English.
        sync_profile(loop, _event("u2", "s2", "我正在开发 知识库问答系统，先做检索"))
        sync_profile(loop, _event("u3", "s3", "I'm working on a trading bot"))
        expect(profiles(store).get("u2", {}).get("current_project") == "知识库问答系统", "phrasing variant captured", profiles(store))
        expect(profiles(store).get("u3", {}).get("current_project") == "trading bot", "english project captured", profiles(store))

        # 3. A program other than GSoC is captured, without the gsoc compat key.
        sync_profile(loop, _event("u4", "s4", "我准备参加 OSPP，方向是 Rust"))
        prof4 = profiles(store).get("u4", {}).get("profile", {})
        program = prof4.get("program", {})
        expect(program.get("name") == "OSPP" and program.get("direction") == "Rust", "novel program captured", prof4)
        expect("gsoc" not in prof4, "non-GSoC program does not set gsoc compat key", prof4)

        # 4. A generic identity/education statement is captured.
        sync_profile(loop, _event("u5", "s5", "我是后端工程师"))
        expect(profiles(store).get("u5", {}).get("profile", {}).get("education") == "后端工程师", "novel education captured", profiles(store))

        # 5. Continuation anchors each user to their own project (isolation).
        expect("电商小程序" in loop._project_context_response(_event("u1", "s1", "继续")), "u1 continuation anchors to its project")
        expect("知识库问答系统" in loop._project_context_response(_event("u2", "s2", "继续")), "u2 continuation anchors to its project")

        # 6. A question must not be mis-captured as a project statement.
        sync_profile(loop, _event("u6", "s6", "Veyra 正在做什么？"))
        expect("u6" not in profiles(store), "interrogative is not captured as a project", profiles(store))

        # 7. The GSoC/Kotlin compat path still works for legacy assertions.
        sync_profile(loop, _event("u7", "s7", "我准备参加GSoC，今年想投Kotlin方向"))
        prof7 = profiles(store).get("u7", {}).get("profile", {})
        expect(prof7.get("gsoc", {}).get("direction") == "Kotlin", "GSoC compat preserved", prof7)

    print("user_profile_generalization_smoke: ok")


if __name__ == "__main__":
    main()
