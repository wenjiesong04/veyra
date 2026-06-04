#!/usr/bin/env python3
"""Offline smoke for execution tiering, compact lookup gating, and commitment guards."""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.commitment_core import CommitmentCore  # noqa: E402
from core.compact_external_lookup import CompactExternalLookup  # noqa: E402
from core.decision_core import DecisionCore  # noqa: E402
from core.execution_tier import TIER_L1_COMPACT_EXTERNAL, classify_execution_tier, compact_lookup_target  # noqa: E402
from core.proactive_intent_planner import ProactiveIntentPlanner  # noqa: E402
from core.provider_repair import repair_provider_error  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import Decision, EventSource, EventType, RiskLevel, Route, VeyraEvent  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> None:
    with TemporaryDirectory() as tmp:
        store = WorldStateStore(root=tmp)
        commitment = CommitmentCore(store)
        text = "帮我找一下王志安在 YouTube 最新一期视频的标题是什么"
        decision = Decision(route=Route.AGENT, risk_level=RiskLevel.R0, reason="test", needs_agent=True)
        expect(classify_execution_tier(text, decision) == TIER_L1_COMPACT_EXTERNAL, "youtube lookup classified as L1")
        expect((compact_lookup_target(text) or {}).get("target", {}).get("creator") == "王志安", "youtube lookup extracts clean creator", compact_lookup_target(text))
        correction = "什么 PyTorch，我说的是柴静在 YouTube 最新一期视频标题"
        expect((compact_lookup_target(correction) or {}).get("target", {}).get("creator") == "柴静", "correction lookup extracts corrected creator", compact_lookup_target(correction))

        fake_ok = {
            "status": "ok",
            "response": "王志安 在 YouTube 的最新视频标题是：示例\n来源：official_youtube_rss\n发布时间：2026-06-03",
            "provider": "youtube_feed_probe",
        }
        with patch.object(CompactExternalLookup, "run", return_value=fake_ok):
            runtime = RuntimeEntity(state_store=store)
            loop = AwarenessLoop(state_store=store, runtime_entity=runtime, commitment_core=commitment)
            event = VeyraEvent(
                type=EventType.USER_MESSAGE,
                source=EventSource(channel="api", user_id="u", session_id="api:u:s1"),
                payload={"text": text},
            )
            result = loop.handle_event(event)
        expect(result.status == "compact_lookup_success", "L1 compact lookup answers without agent", result.status)
        expect("标题" in result.response, "compact response includes title", result.response)
        plan = (result.artifacts.get("decision") or {}).get("model_assist", {}).get("decision_plan", {})
        expect(plan.get("execution_tier") == TIER_L1_COMPACT_EXTERNAL, "decision_plan records L1 tier", plan)

        lookup = CompactExternalLookup()
        bad_search = lookup._success_from_search(
            compact_lookup_target(text) or {},
            {
                "status": "ok",
                "details": {"results": [{"title": "here", "url": "https://duckduckgo.com/", "source": "duckduckgo.com"}]},
            },
            "王志安",
        )
        expect(bad_search.get("status") == "failed", "compact lookup rejects unverified search result", bad_search)

        bare_ok = commitment.process_turn(
            event=VeyraEvent(
                type=EventType.USER_MESSAGE,
                source=EventSource(channel="api", user_id="u", session_id="api:u:s2"),
                payload={"text": "好的"},
            ),
            user_text="好的",
            assistant_response="主回复保留",
            route="direct_answer",
            status="success",
        )
        expect(bare_ok == {}, "bare 好的 without pending does not create commitment", bare_ok)
        planner = ProactiveIntentPlanner(store)
        expect(planner._reminder_topic("以后王志安更新视频提醒我") == "王志安", "reminder topic extracts subject before 提醒我")

        calls: list[dict[str, Any]] = []

        def retry(params: dict[str, Any]) -> str:
            calls.append(params)
            return "ok"

        out = repair_provider_error(
            "unsupported_language: language filtering is not supported",
            retry=retry,
            params={"language": "zh-CN", "query": "test"},
        )
        expect(out == "ok" and "language" not in calls[0], "provider repair strips language and retries", calls)

    print("\nALL architecture harness checks passed.")


if __name__ == "__main__":
    main()
