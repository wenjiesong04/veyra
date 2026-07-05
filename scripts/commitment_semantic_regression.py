#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def artifact(body: dict[str, Any]) -> dict[str, Any]:
    return (body.get("artifacts") or {}).get("commitment") or {}


def message_text(body: dict[str, Any]) -> str:
    return "\n".join([str(body.get("response") or ""), *[str(item) for item in body.get("followup_messages", []) if item]])


def statuses(store: Any, ids: set[str]) -> dict[str, str]:
    state = store.read_json("user_commitments.json")
    items = state.get("commitments") if isinstance(state.get("commitments"), list) else []
    return {str(item.get("commitment_id")): str(item.get("status")) for item in items if isinstance(item, dict) and item.get("commitment_id") in ids}


def commitments(store: Any) -> list[dict[str, Any]]:
    state = store.read_json("user_commitments.json")
    items = state.get("commitments") if isinstance(state.get("commitments"), list) else []
    return [item for item in items if isinstance(item, dict)]


def semantic_change_sets(store: Any) -> list[dict[str, Any]]:
    state = store.read_json("semantic_change_sets.json")
    items = state.get("change_sets") if isinstance(state.get("change_sets"), list) else []
    return [item for item in items if isinstance(item, dict)]


def topic_commitments(store: Any, *, topic: str, kind: str | None = None) -> list[dict[str, Any]]:
    result = []
    for item in commitments(store):
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if kind and item.get("kind") != kind:
            continue
        if str(payload.get("topic") or "") == topic:
            result.append(item)
    return result


def create_commitment(client: TestClient, *, user_id: str, session_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    title = {
        "external_digest": f"外部追踪：{payload.get('topic')}",
        "weather_daily": f"每日天气（{payload.get('location')}）",
        "learning_digest": f"学习资料摘要：{payload.get('topic')}",
    }.get(kind, kind)
    return client.post(
        "/commitments",
        json={
            "kind": kind,
            "status": "active",
            "title": title,
            "user_id": user_id,
            "channel": "api",
            "session_id": session_id,
            "payload": payload,
        },
    ).json()["commitment"]


def main() -> int:
    with TemporaryDirectory(prefix="veyra-commitment-semantic-") as tmp:
        state_root = Path(tmp) / "state"
        agency_root = Path(tmp) / "agency"
        os.environ["VEYRA_STATE_DIR"] = str(state_root)
        os.environ["VEYRA_STATE_ROOT"] = str(state_root)
        os.environ["VEYRA_AGENCY_DIR"] = str(agency_root)
        os.environ["VEYRA_AGENCY_ROOT"] = str(agency_root)
        agency_root.mkdir(parents=True, exist_ok=True)
        (agency_root / "goals.json").write_text("{}", encoding="utf-8")
        (agency_root / "intention_queue.json").write_text("[]", encoding="utf-8")

        import main as app_module  # noqa: E402

        client = TestClient(app_module.app)
        store = app_module.state_store

        user_id = "feishu-semantic-user"
        session_id = "feishu-semantic-session"
        pytorch = create_commitment(
            client,
            user_id=user_id,
            session_id=session_id,
            kind="external_digest",
            payload={"topic": "PyTorch 3.0发布", "query": "PyTorch 3.0 release latest updates", "watchlist_id": "watch_semantic_pytorch"},
        )
        weather = create_commitment(
            client,
            user_id=user_id,
            session_id=session_id,
            kind="weather_daily",
            payload={"location": "贵阳市花溪区", "topic": "weather"},
        )
        learning = create_commitment(
            client,
            user_id=user_id,
            session_id=session_id,
            kind="learning_digest",
            payload={"topic": "深度学习", "phase": "getting_started"},
        )
        ids = {pytorch["commitment_id"], weather["commitment_id"], learning["commitment_id"]}

        cancel = client.post(
            "/events/message",
            json={"text": "取消PyTorch相关内容追踪", "channel": "api", "user_id": user_id, "session_id": session_id},
        ).json()
        cancel_artifact = artifact(cancel)
        current = statuses(store, ids)
        expect(cancel_artifact.get("status") == "cancelled", "specific cancel is a control result", cancel_artifact)
        expect(current[pytorch["commitment_id"]] == "cancelled", "specific cancel stops PyTorch", current)
        expect(current[weather["commitment_id"]] == "active", "specific cancel keeps weather active", current)
        expect(current[learning["commitment_id"]] == "active", "specific cancel keeps learning active", current)
        expect("PyTorch" in message_text(cancel) and "3 个" not in message_text(cancel), "specific cancel response is targeted", cancel)

        before_query = statuses(store, ids)
        query = client.post(
            "/events/message",
            json={"text": "现在是否取消了PyTorch相关内容追踪", "channel": "api", "user_id": user_id, "session_id": session_id},
        ).json()
        after_query = statuses(store, ids)
        query_artifact = artifact(query)
        expect(query_artifact.get("status") == "state_answer", "status query is read-only", query_artifact)
        expect(before_query == after_query, "status query does not mutate commitments", {"before": before_query, "after": after_query})
        expect("已取消" in message_text(query) and "稳定认知模型输出" not in message_text(query), "status answer is deterministic and user-facing", query)

        bare = client.post(
            "/events/message",
            json={"text": "取消", "channel": "api", "user_id": user_id, "session_id": session_id},
        ).json()
        bare_artifact = artifact(bare)
        after_bare = statuses(store, ids)
        expect(bare_artifact.get("status") == "needs_disambiguation", "bare cancel asks for disambiguation", bare_artifact)
        expect(after_bare == after_query, "bare cancel does not stop unrelated active tasks", {"before": after_query, "after": after_bare})
        expect("哪个主动任务" in message_text(bare), "bare cancel names ambiguity", bare)

        nl_user = "nl-semantic-user"
        nl_session = "nl-semantic-session"
        nl_pytorch = create_commitment(
            client,
            user_id=nl_user,
            session_id=nl_session,
            kind="external_digest",
            payload={"topic": "PyTorch", "query": "PyTorch latest updates"},
        )
        still = client.post(
            "/events/message",
            json={"text": "PyTorch 追踪还在吗", "channel": "api", "user_id": nl_user, "session_id": nl_session},
        ).json()
        expect(artifact(still).get("status") == "state_answer", "natural status query stays read-only", still)
        expect(statuses(store, {nl_pytorch["commitment_id"]})[nl_pytorch["commitment_id"]] == "active", "natural status query keeps task active", still)
        expect("还在" in message_text(still) or "运行" in message_text(still), "natural status query answers active state", still)

        stop = client.post(
            "/events/message",
            json={"text": "别再关注 PyTorch 了", "channel": "api", "user_id": nl_user, "session_id": nl_session},
        ).json()
        expect(artifact(stop).get("status") == "cancelled", "natural cancel controls matching task", stop)
        expect(statuses(store, {nl_pytorch["commitment_id"]})[nl_pytorch["commitment_id"]] == "cancelled", "natural cancel stops task", stop)

        pause_user = "pause-semantic-user"
        pause_session = "pause-semantic-session"
        pause_target = create_commitment(
            client,
            user_id=pause_user,
            session_id=pause_session,
            kind="external_digest",
            payload={"topic": "PyTorch", "query": "PyTorch latest updates"},
        )
        pause = client.post(
            "/events/message",
            json={"text": "暂停这个追踪", "channel": "api", "user_id": pause_user, "session_id": pause_session},
        ).json()
        expect(artifact(pause).get("status") == "paused", "short reference pause works when single target exists", pause)
        expect(statuses(store, {pause_target["commitment_id"]})[pause_target["commitment_id"]] == "paused", "pause mutates single target", pause)
        resume = client.post(
            "/events/message",
            json={"text": "恢复刚才那个", "channel": "api", "user_id": pause_user, "session_id": pause_session},
        ).json()
        expect(artifact(resume).get("status") == "resumed", "short reference resume works", resume)
        expect(statuses(store, {pause_target["commitment_id"]})[pause_target["commitment_id"]] == "active", "resume reactivates target", resume)

        shift_user = "shift-semantic-user"
        shift_session = "shift-semantic-session"
        shift_pytorch = create_commitment(
            client,
            user_id=shift_user,
            session_id=shift_session,
            kind="external_digest",
            payload={"topic": "PyTorch", "query": "PyTorch latest updates"},
        )
        before_shift_count = len(commitments(store))
        shift = client.post(
            "/events/message",
            json={
                "text": "接下来我的项目用 PyTorch 已经不如 TensorFlow 了，我可能需要开始学习 TensorFlow。",
                "channel": "api",
                "user_id": shift_user,
                "session_id": shift_session,
            },
        ).json()
        shift_artifact = artifact(shift)
        shift_set = shift_artifact.get("semantic_change_set") if isinstance(shift_artifact.get("semantic_change_set"), dict) else {}
        expect(shift_artifact.get("status") == "semantic_change_proposed", "preference shift becomes a proposed semantic changeset", shift_artifact)
        expect((shift_set.get("execution_decision") or {}).get("mode") == "ask_confirmation", "preference shift requires confirmation", shift_set)
        expect({change.get("entity") for change in shift_set.get("changes", [])} >= {"PyTorch", "TensorFlow"}, "preference shift captures both topics", shift_set)
        expect(statuses(store, {shift_pytorch["commitment_id"]})[shift_pytorch["commitment_id"]] == "active", "preference shift does not pause PyTorch before confirmation", shift)
        expect(len(commitments(store)) == before_shift_count, "preference shift creates no commitment before confirmation", commitments(store))
        expect("状态变化候选" in message_text(shift) and "TensorFlow" in message_text(shift), "preference shift reply asks for confirmation", shift)

        confirm_shift = client.post(
            "/events/message",
            json={"text": "同意", "channel": "api", "user_id": shift_user, "session_id": shift_session},
        ).json()
        confirm_artifact = artifact(confirm_shift)
        expect(confirm_artifact.get("status") == "semantic_change_confirmed", "semantic changeset can be confirmed", confirm_artifact)
        expect(statuses(store, {shift_pytorch["commitment_id"]})[shift_pytorch["commitment_id"]] == "paused", "confirmed shift deprioritizes existing PyTorch tracking", confirm_shift)
        tensorflow_learning = topic_commitments(store, topic="TensorFlow", kind="learning_digest")
        expect(bool(tensorflow_learning) and tensorflow_learning[-1].get("status") == "active", "confirmed shift creates TensorFlow learning tracking", tensorflow_learning)
        expect("已按确认处理" in message_text(confirm_shift), "semantic confirmation response is user-facing", confirm_shift)

        weak_user = "weak-semantic-user"
        weak_session = "weak-semantic-session"
        weak_react = create_commitment(
            client,
            user_id=weak_user,
            session_id=weak_session,
            kind="external_digest",
            payload={"topic": "React", "query": "React latest updates"},
        )
        weak = client.post(
            "/events/message",
            json={"text": "React 可能没那么重要了", "channel": "api", "user_id": weak_user, "session_id": weak_session},
        ).json()
        weak_artifact = artifact(weak)
        expect(weak_artifact.get("status") == "semantic_change_proposed", "weak priority drop becomes proposed change", weak_artifact)
        expect(statuses(store, {weak_react["commitment_id"]})[weak_react["commitment_id"]] == "active", "weak priority drop does not mutate before confirmation", weak)

        interest_before = len(commitments(store))
        interest = client.post(
            "/events/message",
            json={"text": "Rust 最近值得关注", "channel": "api", "user_id": "interest-user", "session_id": "interest"},
        ).json()
        interest_artifact = artifact(interest)
        interest_set = interest_artifact.get("semantic_change_set") if isinstance(interest_artifact.get("semantic_change_set"), dict) else {}
        expect(interest_artifact.get("status") == "semantic_change_proposed", "attention signal becomes proposed change", interest_artifact)
        expect((interest_set.get("proposed_actions") or [{}])[0].get("action") == "create_tracking", "attention signal proposes tracking but does not execute", interest_set)
        expect(len(commitments(store)) == interest_before, "attention signal creates no commitment before confirmation", interest)

        neg_user = "negated-semantic-user"
        neg_session = "negated-semantic-session"
        neg_pytorch = create_commitment(
            client,
            user_id=neg_user,
            session_id=neg_session,
            kind="external_digest",
            payload={"topic": "PyTorch", "query": "PyTorch latest updates"},
        )
        negated = client.post(
            "/events/message",
            json={"text": "不要取消 PyTorch，先加一个 TensorFlow", "channel": "api", "user_id": neg_user, "session_id": neg_session},
        ).json()
        neg_artifact = artifact(negated)
        expect(neg_artifact.get("status") == "semantic_change_proposed", "negated cancel becomes changeset instead of cancel command", neg_artifact)
        expect(statuses(store, {neg_pytorch["commitment_id"]})[neg_pytorch["commitment_id"]] == "active", "negated cancel keeps PyTorch active", negated)
        neg_actions = (neg_artifact.get("semantic_change_set") or {}).get("proposed_actions") or []
        expect(any(item.get("action") == "create_tracking" and item.get("target") == "TensorFlow" for item in neg_actions), "negated cancel only proposes adding TensorFlow", neg_artifact)

        hold_user = "hold-semantic-user"
        hold_session = "hold-semantic-session"
        hold_pytorch = create_commitment(
            client,
            user_id=hold_user,
            session_id=hold_session,
            kind="external_digest",
            payload={"topic": "PyTorch", "query": "PyTorch latest updates"},
        )
        hold_before = len(commitments(store))
        hold = client.post(
            "/events/message",
            json={"text": "PyTorch 不用取消，TensorFlow 也先别订阅", "channel": "api", "user_id": hold_user, "session_id": hold_session},
        ).json()
        hold_artifact = artifact(hold)
        hold_set = hold_artifact.get("semantic_change_set") if isinstance(hold_artifact.get("semantic_change_set"), dict) else {}
        expect(hold_artifact.get("status") == "semantic_change_recorded", "negative preference without action is recorded only", hold_artifact)
        expect(not hold_set.get("proposed_actions"), "hold subscription has no executable proposed action", hold_set)
        expect(statuses(store, {hold_pytorch["commitment_id"]})[hold_pytorch["commitment_id"]] == "active", "hold subscription does not cancel existing PyTorch tracking", hold)
        expect(len(commitments(store)) == hold_before, "hold subscription creates no commitment", hold)
        expect(any(item.get("status") in {"pending_confirmation", "confirmed", "recorded"} for item in semantic_change_sets(store)), "semantic change sets are persisted", semantic_change_sets(store))

    print("commitment semantic regression passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
