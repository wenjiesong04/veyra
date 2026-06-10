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

    print("commitment semantic regression passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
