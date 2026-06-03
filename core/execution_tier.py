from __future__ import annotations

import re
from typing import Any

from interface.event_schema import Decision, Route

# Execution tiers (lowest cost / narrowest context first).
TIER_L0_DIRECT_ANSWER = "L0_direct_answer"
TIER_L1_COMPACT_EXTERNAL = "L1_compact_external_lookup"
TIER_L2_PROBE_VERIFY = "L2_probe_verify"
TIER_L3_SCOPED_AGENT = "L3_scoped_agent_task"
TIER_L4_GUARDED_ACTION = "L4_guarded_action"
TIER_L5_PROACTIVE = "L5_proactive_commitment"

PUSH_INTENT_MARKERS = ("提醒", "订阅", "关注", "跟踪", "留意", "推送", "notify", "remind", "subscribe", "watch", "track", "monitor")
VIDEO_LOOKUP_MARKERS = ("youtube", "youtu.be", "视频", "最新一期", "最新视频", "视频标题", "频道")
LATEST_MARKERS = ("最新", "最近", "当前", "latest", "recent")


def classify_execution_tier(text: str, decision: Decision) -> str:
    lowered = (text or "").lower()
    if _wants_proactive_commitment(text, lowered):
        return TIER_L5_PROACTIVE
    if decision.route in {Route.BLOCK, Route.HUMAN_REVIEW, Route.ROLLBACK}:
        return TIER_L4_GUARDED_ACTION if decision.route != Route.BLOCK else TIER_L4_GUARDED_ACTION
    if decision.route == Route.AGENT and decision.risk_level.value in {"R3", "R4", "R5"}:
        return TIER_L4_GUARDED_ACTION
    if _looks_like_compact_external_video_lookup(text, lowered):
        return TIER_L1_COMPACT_EXTERNAL
    if decision.route == Route.PROBE:
        return TIER_L2_PROBE_VERIFY
    if decision.route == Route.AGENT:
        return TIER_L3_SCOPED_AGENT
    if decision.route == Route.DIRECT_ANSWER:
        return TIER_L0_DIRECT_ANSWER
    return TIER_L0_DIRECT_ANSWER


def _wants_proactive_commitment(text: str, lowered: str) -> bool:
    return any(marker in (text or "") for marker in PUSH_INTENT_MARKERS) or any(marker in lowered for marker in PUSH_INTENT_MARKERS)


def _looks_like_compact_external_video_lookup(text: str, lowered: str) -> bool:
    if not any(marker in lowered for marker in VIDEO_LOOKUP_MARKERS):
        return False
    if not any(marker in (text or "") for marker in LATEST_MARKERS) and "标题" not in text:
        return False
    if _wants_proactive_commitment(text, lowered):
        return False
    return bool(_extract_creator(text))


def _extract_creator(text: str) -> str | None:
    patterns = (
        r"(?:帮我找(?:一下)?|查(?:一下|询)?|看看)?\s*([^\s，,。.!！?？]{2,12})\s*(?:在\s*)?(?:YouTube|youtube|油管)",
        r"(?:YouTube|youtube|油管)\s*(?:上)?\s*([^\s，,。.!！?？]{2,12})",
        r"([^\s，,。.!！?？]{2,12})\s*(?:的)?\s*(?:最新(?:一期)?视频|最新视频|视频标题)",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            creator = match.group(1).strip(" 的了吧吗呢啊？?！!，,。")
            noise = {"什么", "不是", "我说", "帮我", "查一下", "查询", "标题", "最新", "视频", "一期"}
            if creator and creator not in noise and len(creator) >= 2:
                return creator
    return None


def compact_lookup_target(text: str) -> dict[str, Any] | None:
    creator = _extract_creator(text)
    if not creator:
        return None
    return {
        "task_type": "latest_external_video_lookup",
        "user_goal": text,
        "target": {"creator": creator, "platform": "YouTube", "field": "latest_video_title"},
        "source_preference": ["official_youtube_channel", "official_youtube_rss", "web_search"],
        "verification": {
            "must_check_latest": True,
            "include_publish_time": True,
            "include_source": True,
        },
        "answer_language": "zh-CN",
    }
