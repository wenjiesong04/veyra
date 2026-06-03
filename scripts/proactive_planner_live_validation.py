#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.proactive_intent import AUTHORIZATION_STATES, PROACTIVE_INTENT_TYPES, PROACTIVE_NEXT_ACTIONS  # noqa: E402
from core.proactive_intent_planner import ProactiveIntentPlanner  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402


BASE_URL = os.getenv("VEYRA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
CONTROL_TYPES = {"cancel_commitment", "pause_commitment", "resume_commitment"}
CONTROL_ACTIONS = {"cancel_matching_commitments", "pause_matching_commitments", "resume_matching_commitments"}


CASES: list[dict[str, Any]] = [
    {"text": "我要开始学习深度学习了，你能帮我吗？", "expected": {"learning_plan", "goal_start"}},
    {"text": "帮我关注 PyTorch 新版本和重要更新。", "expected": {"track_external_topic"}},
    {"text": "每天早上帮我看看 Veyra 服务是否还在运行。", "expected": {"monitor_local_state"}},
    {"text": "明天上午九点提醒我交作业。", "expected": {"reminder"}},
    {"text": "今天伦敦天气怎么样？", "expected": {"daily_digest"}},
    {"text": "以后都停止推送。", "expected": {"cancel_commitment"}, "control": True},
    {"text": "最近先暂停 PyTorch 更新提醒。", "expected": {"pause_commitment"}, "control": True},
    {"text": "恢复 PyTorch 更新提醒。", "expected": {"resume_commitment"}, "control": True},
    {"text": "帮我留意湾区短租房源。", "expected": {"track_external_topic"}},
    {"text": "帮我持续关注一个未知领域 X。", "expected": {"unknown", "track_external_topic"}},
]


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def get_json(path: str, *, timeout: float = 8.0) -> dict[str, Any]:
    request = Request(f"{BASE_URL}{path}", headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def model_status() -> dict[str, Any]:
    try:
        return get_json("/core/model/status")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return {"status": "unreachable", "configured": False, "warning": str(exc)}


def make_event(text: str, index: int) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="planner-live", user_id="planner-live-user", session_id=f"planner-live-{index}"),
        payload={"text": text},
    )


def validate_intent(intent: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {
        "intent_id",
        "user_id",
        "session_id",
        "channel_id",
        "raw_text",
        "intent_type",
        "topic",
        "entities",
        "desired_outcome",
        "cadence",
        "trigger_condition",
        "information_sources",
        "local_context_needed",
        "external_context_needed",
        "memory_write_needed",
        "requires_user_authorization",
        "authorization_status",
        "risk_level",
        "confidence",
        "proposed_next_action",
        "source",
        "created_at",
    }
    missing = sorted(required - set(intent))
    if missing:
        errors.append(f"missing fields: {missing}")
    if intent.get("intent_type") not in PROACTIVE_INTENT_TYPES:
        errors.append(f"invalid intent_type: {intent.get('intent_type')}")
    if intent.get("proposed_next_action") not in PROACTIVE_NEXT_ACTIONS:
        errors.append(f"invalid proposed_next_action: {intent.get('proposed_next_action')}")
    if intent.get("authorization_status") not in AUTHORIZATION_STATES:
        errors.append(f"invalid authorization_status: {intent.get('authorization_status')}")
    for key in ("entities", "cadence"):
        if not isinstance(intent.get(key), dict):
            errors.append(f"{key} must be object")
    for key in ("information_sources", "local_context_needed", "external_context_needed"):
        if not isinstance(intent.get(key), list):
            errors.append(f"{key} must be list")
    try:
        confidence = float(intent.get("confidence"))
    except (TypeError, ValueError):
        errors.append("confidence must be numeric")
    else:
        if not 0 <= confidence <= 1:
            errors.append("confidence must be between 0 and 1")
    if intent.get("intent_type") in CONTROL_TYPES and intent.get("proposed_next_action") not in CONTROL_ACTIONS:
        errors.append("control intent must route to matching commitment control action")
    if intent.get("intent_type") in CONTROL_TYPES and bool(intent.get("requires_user_authorization")):
        errors.append("control intent must not require new proactive authorization")
    return errors


def main() -> int:
    status = model_status()
    configured = bool(status.get("configured"))
    store = WorldStateStore()
    planner = ProactiveIntentPlanner(store)
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []

    for index, case in enumerate(CASES, start=1):
        intent = planner.plan(user_text=case["text"], event=make_event(case["text"], index))
        payload = intent.to_dict()
        errors = validate_intent(payload)
        expect(not errors, f"case {index} returns valid ProactiveIntent schema", {"errors": errors, "intent": payload})

        source = str(payload.get("source") or "fallback")
        expected = case.get("expected") if isinstance(case.get("expected"), set) else set()
        if expected and payload.get("intent_type") not in expected:
            warnings.append(f"case {index}: expected one of {sorted(expected)}, got {payload.get('intent_type')}")
        if case.get("control"):
            expect(payload.get("intent_type") in CONTROL_TYPES, f"case {index} is control intent", payload)
            expect(payload.get("proposed_next_action") in CONTROL_ACTIONS, f"case {index} cannot create new commitment", payload)
        if payload.get("requires_user_authorization"):
            expect(
                payload.get("proposed_next_action")
                in {"ask_confirmation", "create_goal", "create_commitment_draft", "create_watchlist_draft", *CONTROL_ACTIONS},
                f"case {index} does not authorize direct active push",
                payload,
            )
        if configured and not case.get("control") and source != "model":
            warnings.append(f"case {index}: model configured but planner_source={source}")
        row = {
            "case": index,
            "text": case["text"],
            "intent_type": payload.get("intent_type"),
            "topic": payload.get("topic"),
            "proposed_next_action": payload.get("proposed_next_action"),
            "requires_user_authorization": payload.get("requires_user_authorization"),
            "planner_source": "model" if source == "model" else "fallback",
            "confidence": payload.get("confidence"),
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))

    model_rows = [row for row in rows if row["planner_source"] == "model"]
    fallback_rows = [row for row in rows if row["planner_source"] == "fallback"]
    summary = {
        "status": "success",
        "model_status": {
            "status": status.get("status"),
            "configured": configured,
            "model": status.get("model"),
            "api_key_set": status.get("api_key_set"),
        },
        "planner_source": "model" if model_rows else "fallback",
        "model_count": len(model_rows),
        "fallback_count": len(fallback_rows),
        "case_count": len(rows),
        "warnings": warnings,
        "cases": rows,
    }
    if configured and not model_rows:
        summary["status"] = "warning"
        warnings.append("core model is configured, but no live planner case used model output")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("proactive planner live validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
