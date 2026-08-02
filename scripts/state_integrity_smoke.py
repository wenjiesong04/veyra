#!/usr/bin/env python3
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.commitment_core import CommitmentCore  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.decision_core import DecisionCore  # noqa: E402
from core.perception_layer import PerceptionLayer  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.turn_context_builder import TurnContextBuilder  # noqa: E402
from core.world_state import (  # noqa: E402
    DURABLE_STATE_FILES,
    StateReadOnlyError,
    StateRevisionConflictError,
    WorldStateStore,
)
from interface.event_schema import LoopResult, Route  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.intake_gateway import IntakeGateway  # noqa: E402
from interface.session_mapper import SessionMapper  # noqa: E402
from memory_bridge.local_memory_bridge import LocalMemoryBridge  # noqa: E402
from runtime.retention_policy import RetentionPolicy  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def event(text: str, *, user_id: str = "state-smoke-user", session_id: str = "state-smoke-session") -> Any:
    return EventNormalizer().user_message(
        text=text,
        channel="api",
        user_id=user_id,
        session_id=session_id,
    )


def test_state_writer_and_revisions(root: Path) -> None:
    store = WorldStateStore(root, exclusive_writer=True, writer_owner="state-integrity-smoke")
    first = store.read_json("local_world.json")
    old_updated_at = str(first.get("updated_at") or "")
    store.patch_json("local_world.json", {"probe_marker": "fresh"})
    refreshed = store.read_json("local_world.json")
    expect(refreshed.get("updated_at") != old_updated_at, "state write refreshes envelope updated_at", refreshed)
    expect(int(refreshed.get("_state_revision") or 0) > int(first.get("_state_revision") or 0), "state revision increases", refreshed)

    stale_a = store.read_json("task_state.json")
    stale_b = store.read_json("task_state.json")
    stale_a["writer"] = "a"
    store.write_json("task_state.json", stale_a)
    try:
        stale_b["writer"] = "b"
        store.write_json("task_state.json", stale_b)
        raise AssertionError("stale state write was accepted")
    except StateRevisionConflictError:
        pass
    expect(store.read_json("task_state.json").get("writer") == "a", "stale write is rejected instead of overwriting")

    store.patch_json("counter.json", {"value": 0})

    def increment() -> None:
        for _ in range(100):
            store.mutate_json(
                "counter.json",
                lambda state: {**state, "value": int(state.get("value") or 0) + 1},
            )

    threads = [threading.Thread(target=increment) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    expect(store.read_json("counter.json").get("value") == 800, "transactional mutate_json preserves concurrent updates")

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from core.world_state import WorldStateStore; "
                f"WorldStateStore({str(root)!r}, exclusive_writer=True, writer_owner='competing-writer')"
            ),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    expect(probe.returncode != 0 and "active writer" in probe.stderr, "second process cannot claim state writer lease", probe.stderr)

    reader = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from core.world_state import WorldStateStore; "
                f"print(WorldStateStore({str(root)!r}, read_only=True).read_json('task_state.json').get('writer'))"
            ),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    expect(reader.returncode == 0 and reader.stdout.strip() == "a", "read-only process can inspect state while API writer owns lease", reader.stderr)
    try:
        WorldStateStore(root, read_only=True).patch_json("task_state.json", {"writer": "read-only"})
        raise AssertionError("read-only state view accepted a mutation")
    except StateReadOnlyError:
        pass
    expect(store.read_json("task_state.json").get("writer") == "a", "read-only view rejects mutations")


def test_temp_recovery(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    orphan = root / "runtime" / "task_state.json.deadbeef.tmp"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("partial", encoding="utf-8")
    store = WorldStateStore(root, exclusive_writer=True, writer_owner="temp-recovery-smoke")
    expect(not orphan.exists() and store.recovered_temp_files == 1, "orphan atomic-write temp file is recovered")


def test_corrupt_state_health(root: Path) -> None:
    store = WorldStateStore(root, exclusive_writer=True, writer_owner="corrupt-health-smoke")
    store.path_for("local_world.json").write_text("{broken", encoding="utf-8")
    item = next(entry for entry in store.state_health()["items"] if entry.get("name") == "local_world.json")
    expect(item.get("health_status") == "invalid", "corrupt state is reported invalid instead of fresh", item)


def test_failed_intake_can_retry(root: Path) -> None:
    store = WorldStateStore(root, exclusive_writer=True, writer_owner="intake-retry-smoke")

    class FailingLoop:
        runtime_trace = None

        def handle_event(self, _event: Any) -> Any:
            raise RuntimeError("synthetic intake failure")

    gateway = IntakeGateway(FailingLoop(), state_store=store)  # type: ignore[arg-type]
    for _ in range(2):
        try:
            gateway.receive_message(text="retry me", message_id="retryable-message")
            raise AssertionError("failing intake unexpectedly succeeded")
        except RuntimeError:
            pass
    state = store.read_json("channel_state.json")
    expect(
        not state.get("seen_message_ids")
        and len(state.get("intake_failures", [])) == 2
        and all(
            item.get("message_id") == "retryable-message"
            for item in state.get("intake_failures", [])
            if isinstance(item, dict)
        ),
        "failed intake releases dedupe reservation for retry",
        state,
    )


def test_intake_dedupe_scope_isolation(root: Path) -> None:
    store = WorldStateStore(root, exclusive_writer=True, writer_owner="intake-dedupe-scope-smoke")

    class SuccessfulLoop:
        runtime_trace = None

        def __init__(self) -> None:
            self.events: list[Any] = []

        def handle_event(self, inbound_event: Any) -> LoopResult:
            self.events.append(inbound_event)
            return LoopResult(
                event_id=inbound_event.event_id,
                route=Route.DIRECT_ANSWER,
                status="success",
                response="scope-isolated reply",
                risk_level=RiskLevel.R0,
            )

    raw_message_id = "provider-shared-message-id"
    legacy_event_id = "evt_legacy_other_owner"
    legacy_session_id = "legacy-other-owner-session"
    store.patch_json(
        "channel_state.json",
        {
            "seen_message_ids": {
                raw_message_id: {
                    "event_id": legacy_event_id,
                    "session_id": legacy_session_id,
                    "status": "success",
                }
            }
        },
    )
    loop = SuccessfulLoop()
    gateway = IntakeGateway(loop, state_store=store)  # type: ignore[arg-type]
    request_a = {
        "text": "tenant A",
        "channel": "api",
        "user_id": "tenant:a",
        "session_id": "dialogue",
        "message_id": raw_message_id,
    }
    request_b = {
        "text": "tenant B",
        "channel": "api",
        "user_id": "tenant",
        "session_id": "a:dialogue",
        "message_id": raw_message_id,
    }
    mapped_a = SessionMapper().map("api", "tenant:a", "dialogue")
    mapped_b = SessionMapper().map("api", "tenant", "a:dialogue")
    first_a = gateway.receive_message(**request_a)
    first_b = gateway.receive_message(**request_b)
    replay_b = gateway.receive_message(**request_b)
    replay_text = json.dumps(replay_b, ensure_ascii=False, sort_keys=True)

    expect(
        mapped_a != mapped_b
        and first_a.get("status") == "delivered"
        and first_b.get("status") == "delivered"
        and len(loop.events) == 2,
        "framed session and intake scopes keep delimiter-collision identities independent",
        {
            "mapped_a": mapped_a,
            "mapped_b": mapped_b,
            "first_a": first_a,
            "first_b": first_b,
            "handled_count": len(loop.events),
        },
    )
    expect(
        replay_b.get("status") == "duplicate"
        and replay_b.get("message_id") == raw_message_id
        and replay_b.get("session_id") == mapped_b
        and replay_b.get("previous", {}).get("event_id")
        == first_b.get("event", {}).get("event_id")
        and replay_b.get("previous", {}).get("session_id") == mapped_b
        and len(loop.events) == 2,
        "same exact owner replay is deduplicated while preserving the raw public message id",
        replay_b,
    )
    expect(
        first_a.get("event", {}).get("event_id") not in replay_text
        and mapped_a not in replay_text
        and legacy_event_id not in replay_text
        and legacy_session_id not in replay_text,
        "duplicate response cannot disclose another owner or legacy reservation",
        replay_b,
    )

    state = store.read_json("channel_state.json")
    seen = state.get("seen_message_ids", {})
    scoped_entries = {
        key: value
        for key, value in seen.items()
        if isinstance(key, str) and key.startswith("intake-dedupe-v2-")
    }
    scoped_owners = {
        (
            str((value.get("owner") or {}).get("user_id") or ""),
            str((value.get("owner") or {}).get("session_id") or ""),
        )
        for value in scoped_entries.values()
        if isinstance(value, dict)
    }
    expect(
        raw_message_id in seen
        and len(scoped_entries) == 2
        and scoped_owners == {("tenant:a", mapped_a), ("tenant", mapped_b)}
        and all(
            value.get("scope_version") == "veyra-intake-dedupe-v2"
            for value in scoped_entries.values()
            if isinstance(value, dict)
        ),
        "legacy raw keys are ignored and new reservations persist exact versioned owner envelopes",
        seen,
    )


def test_fact_and_memory_boundaries(root: Path) -> None:
    store = WorldStateStore(root, exclusive_writer=True, writer_owner="boundary-smoke")
    perception = PerceptionLayer(store, model_assist_enabled=False)
    patch = perception.interpret_probe_result(
        {
            "probe": "port_probe",
            "target": "127.0.0.1:8000",
            "host": "127.0.0.1",
            "port": 8000,
            "status": "ok",
            "summary": "port 8000 is reachable",
            "confidence": 0.91,
            "ttl_seconds": 30,
        }
    )
    observation = store.read_json("local_world.json").get("probes", {}).get("port_probe", {})
    claim = patch.get("belief.claims", [{}])[0]
    expect(
        observation.get("fact_kind") == "observation" and observation.get("expires_at"),
        "probe observation stores item-level expiry",
        observation,
    )
    expect(
        "model_interpretation" not in observation and claim.get("claim_kind") == "observed",
        "raw observation stays separate from derived model interpretation",
        {"observation": observation, "claim": claim},
    )
    expect(store.read_json("risk_policy.json").get("ttl_seconds") == 0, "durable risk policy does not expire")
    config = store.read_json("agent_config.json")
    expect(
        config.get("ttl_seconds") == 0
        and config.get("config_revision") == 1
        and bool(config.get("loaded_at"))
        and len(str(config.get("source_hash") or "")) == 64,
        "config hard facts carry durable revision and source metadata",
        config,
    )
    expect("levels" not in store.read_json("risk_state.json"), "volatile risk state does not embed policy catalog")
    schema = store.read_json("state_schema.json")
    schema_ids = {
        item.get("id")
        for item in schema.get("state_definitions", [])
        if isinstance(item, dict)
    }
    expected_cognitive_ids = {
        "context_binding_state",
        "cognitive_loop_state",
        "event_inbox_state",
        "situation_state",
        "general_situation_state",
        "suggestion_outbox",
    }
    expect(
        schema.get("version") == 2
        and "risk_policy" in schema_ids
        and expected_cognitive_ids.issubset(schema_ids),
        "persisted state schema includes the governed cognition graph",
        sorted(schema_ids),
    )
    expect(
        {
            "context_binding_state.json",
            "cognitive_loop_state.json",
        }.issubset(DURABLE_STATE_FILES)
        and store.read_json("context_binding_state.json").get("ttl_seconds") == 0
        and store.read_json("cognitive_loop_state.json").get("ttl_seconds") == 0,
        "context binding and cognitive loop state remain durable across restart",
    )
    hermes_decision = DecisionCore(store)._rule_decide("Hermes runtime 当前状态怎么样", [])
    expect(
        hermes_decision.selected_probe == "hermes",
        "specific runtime status selects its probe before generic system status",
        hermes_decision.to_dict(),
    )

    bridge = LocalMemoryBridge(store)
    bridge.write_patch(
        {
            "user_id": "memory-boundary-user",
            "session_id": "memory-boundary",
            "memory_type": "user_preference",
            "preference": {"scope": "response_style", "source_text": "回答直接一点"},
            "summary": "用户偏好直接回答",
        },
        provider="local",
    )
    memory = store.read_json("agent_memory.json")
    expect(
        memory.get("memory_class") == "soft"
        and memory.get("ttl_seconds") == 0
        and memory.get("items", [{}])[-1].get("memory_namespace") == "agent_bridge",
        "soft memory uses its own durable namespace",
        memory,
    )

    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    active = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    summary = TurnContextBuilder(store)._task_summary(
        {
            "short_term_memory": [
                {
                    "user_id": "memory-boundary-user",
                    "session_id": "memory-boundary",
                    "summary": "expired",
                    "expires_at": expired,
                },
                {
                    "user_id": "memory-boundary-user",
                    "session_id": "memory-boundary",
                    "summary": "active",
                    "expires_at": active,
                },
            ]
        },
        user_id="memory-boundary-user",
        session_id="memory-boundary",
    )
    summaries = [item.get("summary") for item in summary.get("short_term_memory", []) if isinstance(item, dict)]
    expect(summaries == ["active"], "expired short-term memory is excluded from turn context", summary)


def test_retention(root: Path) -> None:
    previous = os.environ.get("VEYRA_ACTION_RECORD_RETENTION_LIMIT")
    os.environ["VEYRA_ACTION_RECORD_RETENTION_LIMIT"] = "10"
    try:
        store = WorldStateStore(root, exclusive_writer=True, writer_owner="retention-smoke")
        for index in range(25):
            store.append_jsonl(
                "action_record.jsonl",
                {"route": "state_integrity_retention", "status": "success", "artifacts": {"index": index}},
            )
        archives = list((root / "archive" / "retention").glob("action_record-*.jsonl.gz"))
        rows = store.read_jsonl("action_record.jsonl", limit=100)
        expect(len(rows) <= 10, "action stream remains bounded after batch rotation", len(rows))
        expect(1 <= len(archives) <= 4, "batch rotation avoids one archive per appended row", archives)
        expect(all(gzip.open(path, "rb").read() for path in archives), "rotated archives are readable gzip JSONL")

        archive_root = root / "archive" / "retention"
        for index in range(12):
            (archive_root / f"event_log-2026-07-24T1200{index:02d}+0000-{index:08d}.jsonl").write_text(
                json.dumps({"index": index}) + "\n",
                encoding="utf-8",
            )
        result = RetentionPolicy(store).compact_archives(min_files_per_group=10)
        compacted = list(archive_root.glob("event_log-2026-07-24-compacted-*.jsonl.gz"))
        expect(result.get("files_compacted") == 12 and len(compacted) == 1, "legacy tiny archives compact by stream/day", result)
        with gzip.open(compacted[0], "rt", encoding="utf-8") as handle:
            expect(len(handle.read().splitlines()) == 12, "archive compaction preserves every JSONL row")
    finally:
        if previous is None:
            os.environ.pop("VEYRA_ACTION_RECORD_RETENTION_LIMIT", None)
        else:
            os.environ["VEYRA_ACTION_RECORD_RETENTION_LIMIT"] = previous


def test_model_down_state_answers(root: Path) -> None:
    store = WorldStateStore(root, exclusive_writer=True, writer_owner="state-answer-smoke")
    runtime = RuntimeEntity(store)
    commitments = CommitmentCore(store)
    loop = AwarenessLoop(store, runtime, commitment_core=commitments)
    store.patch_json(
        "executor_state.json",
        {"selected_agent": "openclaw", "status": "available", "connected": True},
    )
    store.patch_json("attention_state.json", {"focus": ["veyra_project", "deployment"]})
    commitments.create_commitment(
        {
            "kind": "external_digest",
            "status": "active",
            "title": "PyTorch 新版本追踪",
            "user_id": "state-smoke-user",
            "session_id": "state-smoke-session",
            "channel": "api",
            "payload": {"topic": "PyTorch"},
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    overview = loop.handle_event(event("当前 Veyra 本地状态怎么样？"))
    expect(
        overview.route.value == "direct_answer"
        and "本地状态文件" in overview.response
        and "执行器：openclaw" in overview.response,
        "model-down local status is answered from state",
        overview.to_dict(),
    )
    workload = loop.handle_event(event("现在有哪些未完成任务、关注主题和可用执行器？"))
    expect(
        workload.route.value == "direct_answer"
        and "未完成事项" in workload.response
        and "当前关注" in workload.response,
        "model-down workload summary is answered from state",
        workload.to_dict(),
    )
    commitment_topics_after_queries = [
        str((item.get("payload") or {}).get("topic") or "")
        for item in commitments.list_commitments(user_id="state-smoke-user")
        if isinstance(item, dict)
    ]
    expect(
        commitment_topics_after_queries == ["PyTorch"],
        "state-grounded answers never create commitments from query wording",
        commitment_topics_after_queries,
    )
    tracking = loop.handle_event(event("PyTorch 现在还在追踪吗？"))
    expect(
        tracking.route.value == "direct_answer"
        and "PyTorch" in tracking.response
        and ("运行" in tracking.response or "还在" in tracking.response),
        "model-down commitment status is answered from state",
        tracking.to_dict(),
    )
    latest_event = store.read_jsonl("event_log.jsonl", limit=1)[0]
    event_payload = latest_event.get("event", {}).get("payload", {})
    expect(
        "text" not in event_payload and event_payload.get("text_sha256") and event_payload.get("text_preview"),
        "event stream persists redacted text metadata instead of full payload",
        latest_event,
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-state-writer-") as tmp:
        test_state_writer_and_revisions(Path(tmp) / "state")
    with TemporaryDirectory(prefix="veyra-temp-recovery-") as tmp:
        test_temp_recovery(Path(tmp) / "state")
    with TemporaryDirectory(prefix="veyra-corrupt-health-") as tmp:
        test_corrupt_state_health(Path(tmp) / "state")
    with TemporaryDirectory(prefix="veyra-intake-retry-") as tmp:
        test_failed_intake_can_retry(Path(tmp) / "state")
    with TemporaryDirectory(prefix="veyra-intake-dedupe-scope-") as tmp:
        test_intake_dedupe_scope_isolation(Path(tmp) / "state")
    with TemporaryDirectory(prefix="veyra-boundaries-") as tmp:
        test_fact_and_memory_boundaries(Path(tmp) / "state")
    with TemporaryDirectory(prefix="veyra-retention-") as tmp:
        test_retention(Path(tmp) / "state")
    with TemporaryDirectory(prefix="veyra-state-answer-") as tmp:
        test_model_down_state_answers(Path(tmp) / "state")
    print("state integrity smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
