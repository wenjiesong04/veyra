#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.commitment_core import CommitmentCore  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402


BASE_URL = os.getenv("VEYRA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def request_json(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise AssertionError(f"{method} {path} failed against {BASE_URL}: {exc}") from exc


def get_json(path: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0) -> dict[str, Any]:
    suffix = f"?{urlencode(params)}" if params else ""
    return request_json("GET", f"{path}{suffix}", None, timeout=timeout)


def post_message(text: str, *, user_id: str, session_id: str, index: int) -> dict[str, Any]:
    return request_json(
        "POST",
        "/events/message",
        {
            "text": text,
            "channel": "api",
            "user_id": user_id,
            "session_id": session_id,
            "message_id": f"runtime-e2e-{session_id}-{index}",
            "metadata": {"smoke": "runtime_e2e_dialogue", "case": index},
        },
        timeout=120.0,
    )


def artifact(body: dict[str, Any]) -> dict[str, Any]:
    return (body.get("artifacts") or {}).get("commitment") or {}


def message_text(body: dict[str, Any]) -> str:
    return "\n".join([str(body.get("response") or ""), *[str(item) for item in body.get("followup_messages", []) if item]])


def commitments(store: WorldStateStore, user_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
    state = store.read_json("user_commitments.json")
    items = state.get("commitments") if isinstance(state.get("commitments"), list) else []
    rows = [item for item in items if isinstance(item, dict) and item.get("user_id") == user_id]
    if status:
        rows = [item for item in rows if item.get("status") == status]
    return rows


def find_commitment(store: WorldStateStore, user_id: str, *, kind: str, topic: str | None = None, status: str | None = None) -> dict[str, Any]:
    topic_lower = (topic or "").lower()
    for item in reversed(commitments(store, user_id, status=status)):
        if item.get("kind") != kind:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        haystack = " ".join(str(part or "").lower() for part in (item.get("title"), payload.get("topic"), payload.get("query")))
        if not topic_lower or topic_lower in haystack:
            return item
    return {}


def proactive_counts(store: WorldStateStore) -> tuple[int, int]:
    intents = store.read_json("proactive_intents.json").get("intents", [])
    proposals = store.read_json("self_improvement_proposals.json").get("proposals", [])
    return (
        len(intents) if isinstance(intents, list) else 0,
        len(proposals) if isinstance(proposals, list) else 0,
    )


def main() -> int:
    health = get_json("/health", timeout=12.0)
    expect(health.get("status") in {"healthy", "degraded", "critical"}, "Veyra health endpoint reachable", health)

    suffix = uuid4().hex[:8]
    user_id = f"runtime-e2e-user-{suffix}"
    session_id = f"dialogue-{suffix}"
    # The live API is the sole writer for the production state root. This smoke
    # only observes the resulting state and must not contend for its writer lease.
    store = WorldStateStore(read_only=True)

    learning = post_message("现在我要开始学习深度学习了，你能帮我吗？", user_id=user_id, session_id=session_id, index=1)
    learning_artifact = artifact(learning)
    expect(learning_artifact.get("status") == "goal_recorded", "learning request records a goal", learning_artifact)
    expect("深度学习" in message_text(learning) and "启动" in message_text(learning), "learning response gives a start plan", learning)
    pending_digest = learning_artifact.get("commitment") if isinstance(learning_artifact.get("commitment"), dict) else {}
    expect(pending_digest.get("kind") == "learning_digest" and pending_digest.get("status") == "pending_confirmation", "learning digest is pending before consent", pending_digest)
    expect((learning_artifact.get("watchlist_draft") or {}).get("status") == "pending_confirmation", "learning watchlist draft is pending", learning_artifact)
    expect(not commitments(store, user_id, status="active"), "learning request does not directly activate push", commitments(store, user_id))

    confirm = post_message("可以，每天早上给我推一点", user_id=user_id, session_id=session_id, index=2)
    confirm_artifact = artifact(confirm)
    active_learning = find_commitment(store, user_id, kind="learning_digest", topic="深度学习", status="active")
    schedule = active_learning.get("schedule") if isinstance(active_learning.get("schedule"), dict) else {}
    expect(confirm_artifact.get("status") == "confirmed" and active_learning, "pending learning commitment is authorized", confirm_artifact)
    expect(schedule.get("kind") == "daily" and schedule.get("time_local") == "08:00" and schedule.get("timezone") == "Asia/Shanghai", "daily morning schedule is stored", active_learning)
    channel_config = get_json("/channels/api/config")
    expect(channel_config.get("status") == "success" and (channel_config.get("config") or {}).get("delivery"), "delivery channel is diagnosable", channel_config)

    track = post_message("帮我关注 PyTorch 新版本", user_id=user_id, session_id=session_id, index=3)
    track_artifact = artifact(track)
    pytorch_pending = find_commitment(store, user_id, kind="external_digest", topic="PyTorch", status="pending_confirmation")
    expect((track_artifact.get("intent") or {}).get("intent_type") == "track_external_topic", "PyTorch request is track_external_topic", track_artifact)
    expect((track_artifact.get("watchlist_draft") or {}).get("status") == "pending_confirmation" and pytorch_pending, "PyTorch watchlist and commitment draft are created", track_artifact)
    expect("回复" in message_text(track) and "好的" in message_text(track), "PyTorch tracking asks for push authorization", track)

    ids_before_pause = {str(item.get("commitment_id")) for item in commitments(store, user_id)}
    pause = post_message("最近先暂停 PyTorch 更新提醒", user_id=user_id, session_id=session_id, index=4)
    ids_after_pause = {str(item.get("commitment_id")) for item in commitments(store, user_id)}
    pytorch_paused = find_commitment(store, user_id, kind="external_digest", topic="PyTorch", status="paused")
    expect(pytorch_paused and ids_before_pause == ids_after_pause, "pause matches PyTorch without creating a new task", {"pause": pause, "commitments": commitments(store, user_id)})

    resume = post_message("恢复 PyTorch 更新提醒", user_id=user_id, session_id=session_id, index=5)
    pytorch_active = find_commitment(store, user_id, kind="external_digest", topic="PyTorch", status="active")
    expect(pytorch_active and artifact(resume).get("status") == "resumed", "PyTorch reminder resumes from paused to active", {"resume": resume, "commitment": pytorch_active})

    cancel = post_message("以后都停止推送", user_id=user_id, session_id=session_id, index=6)
    active_after_cancel = commitments(store, user_id, status="active")
    commitment_reader = CommitmentCore(store)
    pushable_after_cancel = [
        item
        for item in commitments(store, user_id)
        if commitment_reader.pushable_reason(item)[0]
    ]
    expect(not active_after_cancel, "all active proactive commitments are cancelled", {"cancel": cancel, "commitments": commitments(store, user_id)})
    expect(not pushable_after_cancel, "cancelled commitments are not pushable", pushable_after_cancel)

    counts_before_identity = proactive_counts(store)
    identity = post_message("你现在是 Veyra 还是 OpenClaw？", user_id=user_id, session_id=session_id, index=7)
    expect("Veyra" in str(identity.get("response") or "") and "OpenClaw" in str(identity.get("response") or ""), "identity answer is direct Veyra boundary", identity.get("response"))
    expect(not artifact(identity), "identity answer creates no proactive draft", artifact(identity))
    expect(proactive_counts(store) == counts_before_identity, "identity answer records no proactive intent/proposal", {"before": counts_before_identity, "after": proactive_counts(store)})

    recall = post_message("你记得我现在在学什么吗？", user_id=user_id, session_id=session_id, index=8)
    expect("深度学习" in str(recall.get("response") or ""), "learning goal is recalled from Veyra memory/state", recall.get("response"))

    print(
        json.dumps(
            {
                "status": "success",
                "user_id": user_id,
                "session_id": session_id,
                "learning_commitment": active_learning.get("commitment_id"),
                "pytorch_commitment": pytorch_active.get("commitment_id"),
                "final_active_commitments": len(active_after_cancel),
                "health_at_start": health.get("status"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("runtime e2e dialogue smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
