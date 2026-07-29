#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.event_schema import VeyraTaskPacket  # noqa: E402
from interface.openclaw_adapter import OpenClawAdapter  # noqa: E402
from memory_bridge.local_memory_bridge import LocalMemoryBridge  # noqa: E402


class FakeMemoryAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.reads: list[str] = []
        self.connection_checks = 0

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        return ExecutionResult(task_id=task_packet.task_id, executor="fake", status="success", result="ok")

    def connection_status(self) -> dict[str, Any]:
        self.connection_checks += 1
        return {"status": "available", "connected": True}

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        self.reads.append(session_id)
        return {"status": "success", "summary": f"external memory for {session_id}", "freshness": "fresh", "trust": "fake"}

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> dict[str, Any]:
        self.writes.append(memory_patch)
        return {"status": "submitted", "memory_id": "fake-memory-id"}


class BlockedMemoryAdapter(FakeMemoryAdapter):
    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        self.reads.append(session_id)
        return {
            "status": "error",
            "summary": "",
            "freshness": "stale",
            "trust": "untrusted",
        }

    def write_memory_patch(
        self,
        memory_patch: dict[str, Any],
    ) -> dict[str, Any]:
        self.writes.append(memory_patch)
        return {
            "status": "blocked",
            "reason": "provider_write_not_certified",
        }


class RecordingReasoning:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def memory_assist(
        self,
        *,
        session_id: str,
        focus: list[str],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "session_id": session_id,
                "focus": focus,
                "candidates": candidates,
            }
        )
        return {"status": "skipped"}


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> int:
    with TemporaryDirectory(prefix="veyra-memory-quality-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        fake = FakeMemoryAdapter()
        reasoning = RecordingReasoning()
        bridge = LocalMemoryBridge(
            store,
            adapter_resolver=lambda: fake,
            adapter_getter=lambda provider: fake,
            provider_names=lambda: ["openclaw"],
            reasoning=reasoning,  # type: ignore[arg-type]
        )

        patch = {
            "user_id": "user-a",
            "session_id": "session-a",
            "task": "remember deep learning plan",
            "result": "start with basics",
            "trust": "verified",
        }
        first = bridge.write_patch(patch, provider="openclaw")
        second = bridge.write_patch(patch, provider="openclaw")
        expect(first["status"] == "written" and not first["deduped"], "first memory write stored", first)
        expect(second["status"] == "written" and second["deduped"], "duplicate memory write deduped", second)

        state = store.read_json("agent_memory.json")
        items = state.get("items") if isinstance(state.get("items"), list) else []
        expect(len(items) == 1, "duplicate memory collapsed to one item", items)
        item = items[0]
        expect(bool(item.get("memory_id")) and item.get("merged_count") == 2, "memory item has stable id and merge count", item)
        expect((item.get("quality") or {}).get("score", 0) > 0.8, "memory quality score recorded", item)
        expect(fake.writes and fake.writes[-1]["summary"], "external adapter receives normalized patch", fake.writes)
        expect(
            fake.writes[-1].get("session_id", "").startswith("veyra-memory-v2-")
            and "user_id" not in fake.writes[-1],
            "external adapter receives only a derived owner-session scope",
            fake.writes[-1],
        )

        other_session = bridge.write_patch(
            {
                "user_id": "user-a",
                "session_id": "session-b",
                "task": "session-b-only",
                "result": "private-a-b",
            },
            provider="local",
        )
        other_user = bridge.write_patch(
            {
                "user_id": "user-b",
                "session_id": "session-a",
                "task": "user-b-only",
                "result": "private-b-a",
            },
            provider="local",
        )
        expect(other_session["status"] == "written" and other_user["status"] == "written", "three scope quadrants stored")
        expect(
            len(
                {
                    first["item"]["memory_id"],
                    other_session["item"]["memory_id"],
                    other_user["item"]["memory_id"],
                }
            )
            == 3,
            "memory id includes owner and session scope",
        )
        case_variant_id = bridge._memory_id(  # noqa: SLF001 - invariant smoke
            {**patch, "user_id": "USER-A"},
            "openclaw",
        )
        expect(
            case_variant_id != first["item"]["memory_id"],
            "owner identity remains case-sensitive in memory id",
            case_variant_id,
        )
        expect(
            len(
                {
                    bridge._scoped_session_id("user-a", "session-a"),  # noqa: SLF001
                    bridge._scoped_session_id("user-a", "session-b"),  # noqa: SLF001
                    bridge._scoped_session_id("user-b", "session-a"),  # noqa: SLF001
                }
            )
            == 3,
            "external provider scopes bind both owner and session",
        )
        try:
            bridge._scoped_session_id(  # noqa: SLF001
                "scope-a\x00scope-b",
                "scope-c",
            )
        except ValueError:
            control_rejected = True
        else:
            control_rejected = False
        expect(
            control_rejected,
            "scope components reject embedded control characters",
        )
        expect(
            bridge._scoped_session_id(  # noqa: SLF001
                "scope-a",
                "scope-b:scope-c",
            )
            != bridge._scoped_session_id(  # noqa: SLF001
                "scope-a:scope-b",
                "scope-c",
            ),
            "length-framed scope identity has no delimiter collision",
        )

        state = store.read_json("agent_memory.json")
        state["items"].extend(
            [
                {
                    "memory_id": "legacy-missing-user",
                    "session_id": "session-a",
                    "patch": {"session_id": "session-a", "summary": "legacy secret"},
                },
                {
                    "memory_id": "legacy-missing-session",
                    "user_id": "user-a",
                    "patch": {"user_id": "user-a", "summary": "legacy sessionless secret"},
                },
                {
                    "memory_id": "conflicting-envelope",
                    "user_id": "user-a",
                    "session_id": "session-a",
                    "patch": {
                        "user_id": "user-b",
                        "session_id": "session-a",
                        "summary": "conflicting secret",
                    },
                },
            ]
        )
        store.write_json("agent_memory.json", state)

        a_a = bridge.read_summary("session-a", provider="local", user_id="user-a")
        a_b = bridge.read_summary("session-b", provider="local", user_id="user-a")
        b_a = bridge.read_summary("session-a", provider="local", user_id="user-b")
        expect(len(a_a["summary"]) == 1 and "deep learning" in str(a_a["summary"]), "user A session A is exact")
        expect(len(a_b["summary"]) == 1 and "session-b-only" in str(a_b["summary"]), "user A session B is exact")
        expect(len(b_a["summary"]) == 1 and "user-b-only" in str(b_a["summary"]), "user B session A is exact")
        combined = str([a_a["summary"], a_b["summary"], b_a["summary"]])
        expect("legacy secret" not in combined and "sessionless secret" not in combined, "legacy unowned memory is unreadable")
        expect("conflicting secret" not in combined, "conflicting owner envelope fails closed")
        expect(
            all(
                len(call["candidates"]) == 1
                for call in reasoning.calls[-3:]
            ),
            "model relevance sees only the exact owner-session partition",
            reasoning.calls[-3:],
        )

        calls_before_miss = len(reasoning.calls)
        focus_miss = bridge.read_summary(
            "session-a",
            ["not-present-anywhere"],
            provider="local",
            user_id="user-a",
        )
        expect(focus_miss["summary"] == [], "focus miss returns empty instead of all scoped memory", focus_miss)
        expect(len(reasoning.calls) == calls_before_miss, "empty candidates never reach model relevance")

        expired = {
            "memory_id": "expired",
            "user_id": "user-a",
            "session_id": "old",
            "patch": {"user_id": "user-a", "session_id": "old", "task": "old", "result": "old"},
            "provider": "local",
            "quality": {"score": 0.1},
            "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }
        state = store.read_json("agent_memory.json")
        state["items"].append(expired)
        store.write_json("agent_memory.json", state)
        bridge.write_patch(
            {
                "user_id": "user-a",
                "session_id": "session-a",
                "task": "new stable note",
                "result": "keep",
            },
            provider="local",
        )
        refreshed_items = store.read_json("agent_memory.json").get("items", [])
        expect(not any(entry.get("memory_id") == "expired" for entry in refreshed_items if isinstance(entry, dict)), "expired memory pruned on write", refreshed_items)

        summary = bridge.read_summary("session-a", ["deep learning"], provider="openclaw", user_id="user-a")
        expect(summary["external_summary"]["status"] == "success" and summary["summary"], "memory summary includes local and external memory", summary)
        expect(
            fake.reads[-1] == fake.writes[0]["session_id"],
            "external reads and writes use the same stable derived scope",
            {"read": fake.reads[-1], "write": fake.writes[0]["session_id"]},
        )
        reads_before_all = len(fake.reads)
        bridge.read_summary(
            "session-a",
            provider="all",
            user_id="user-a",
            model_assist=False,
            external_read=True,
        )
        writes_before_all = len(fake.writes)
        bridge.write_patch(
            {
                "user_id": "user-a",
                "session_id": "session-a",
                "task": "all-provider-alias-check",
                "result": "one external submission",
            },
            provider="all",
        )
        expect(
            len(fake.reads) == reads_before_all + 1
            and len(fake.writes) == writes_before_all + 1,
            "provider all deduplicates selected and concrete aliases",
            {
                "reads": fake.reads[reads_before_all:],
                "writes": fake.writes[writes_before_all:],
            },
        )
        adapter_a = FakeMemoryAdapter()
        adapter_b = FakeMemoryAdapter()
        selected_resolutions = 0

        def changing_selected() -> FakeMemoryAdapter:
            nonlocal selected_resolutions
            selected_resolutions += 1
            return adapter_a if selected_resolutions == 1 else adapter_b

        snapshot_bridge = LocalMemoryBridge(
            store,
            adapter_resolver=changing_selected,
            adapter_getter=lambda provider: (
                adapter_b if provider == "provider-b" else None
            ),
            provider_names=lambda: ["provider-b"],
            reasoning=reasoning,  # type: ignore[arg-type]
        )
        fanout = snapshot_bridge.write_patch(
            {
                "user_id": "user-a",
                "session_id": "session-a",
                "task": "provider-binding-snapshot",
                "result": "dispatch each bound adapter once",
            },
            provider="all",
        )
        expect(
            selected_resolutions == 1
            and len(adapter_a.writes) == 1
            and len(adapter_b.writes) == 1
            and fanout.get("status") == "written",
            "provider all dispatches one immutable adapter snapshot",
            {
                "selected_resolutions": selected_resolutions,
                "a_writes": len(adapter_a.writes),
                "b_writes": len(adapter_b.writes),
                "fanout": fanout,
            },
        )
        diagnostic_adapter_a = FakeMemoryAdapter()
        diagnostic_adapter_b = FakeMemoryAdapter()
        diagnostic_resolutions = 0

        def changing_diagnostic_selected() -> FakeMemoryAdapter:
            nonlocal diagnostic_resolutions
            diagnostic_resolutions += 1
            return (
                diagnostic_adapter_a
                if diagnostic_resolutions == 1
                else diagnostic_adapter_b
            )

        diagnostic_snapshot_bridge = LocalMemoryBridge(
            store,
            adapter_resolver=changing_diagnostic_selected,
            adapter_getter=lambda provider: (
                diagnostic_adapter_b
                if provider == "provider-b"
                else None
            ),
            provider_names=lambda: ["provider-b"],
            reasoning=reasoning,  # type: ignore[arg-type]
        )
        diagnostic_fanout = diagnostic_snapshot_bridge.provider_diagnostics(
            provider="all",
            session_id="session-a",
            user_id="user-a",
            write_probe=True,
            record=False,
            persist=False,
            active_probe=True,
        )
        expect(
            diagnostic_resolutions == 1
            and len(diagnostic_adapter_a.writes) == 1
            and len(diagnostic_adapter_b.writes) == 1
            and len(diagnostic_adapter_a.reads) == 2
            and len(diagnostic_adapter_b.reads) == 2,
            "provider diagnostics uses one immutable adapter snapshot",
            {
                "selected_resolutions": diagnostic_resolutions,
                "a_writes": len(diagnostic_adapter_a.writes),
                "b_writes": len(diagnostic_adapter_b.writes),
                "a_reads": len(diagnostic_adapter_a.reads),
                "b_reads": len(diagnostic_adapter_b.reads),
                "fanout": diagnostic_fanout,
            },
        )

        blocked_adapter = BlockedMemoryAdapter()
        blocked_bridge = LocalMemoryBridge(
            store,
            adapter_resolver=lambda: blocked_adapter,
            provider_names=lambda: [],
            reasoning=reasoning,  # type: ignore[arg-type]
        )
        blocked_summary = blocked_bridge.read_summary(
            "session-a",
            provider="all",
            user_id="user-a",
            model_assist=False,
        )
        blocked_external_write = blocked_bridge.write_patch(
            {
                "user_id": "user-a",
                "session_id": "session-a",
                "task": "blocked-provider",
                "result": "local copy only",
            },
            provider="selected",
        )
        expect(
            blocked_summary.get("external_summary", {}).get("status")
            == "error",
            "provider all does not report success when every read fails",
            blocked_summary,
        )
        expect(
            blocked_external_write.get("status") == "partial"
            and blocked_external_write.get("local_status") == "written"
            and blocked_external_write.get("external_write", {}).get(
                "status"
            )
            == "blocked",
            "blocked external write cannot masquerade as fully written",
            blocked_external_write,
        )

        before_diagnostics_state = store.read_json("agent_memory.json")
        before_diagnostics_log = store.read_jsonl("memory_log.jsonl")
        diagnostic = bridge.provider_diagnostics(
            provider="openclaw",
            session_id="session-a",
            user_id="user-a",
            record=False,
            persist=False,
        )
        expect("external memory for" not in str(diagnostic), "read-only diagnostics do not expose memory content", diagnostic)
        expect(
            store.read_json("agent_memory.json") == before_diagnostics_state
            and store.read_jsonl("memory_log.jsonl") == before_diagnostics_log,
            "read-only diagnostics do not change state or audit logs",
        )

        pure_read_counts = (
            len(reasoning.calls),
            len(fake.reads),
            fake.connection_checks,
        )
        pure_summary = bridge.read_summary(
            "session-a",
            provider="openclaw",
            user_id="user-a",
            model_assist=False,
            external_read=False,
        )
        pure_diagnostics = bridge.provider_diagnostics(
            provider="openclaw",
            session_id="session-a",
            user_id="user-a",
            record=False,
            persist=False,
            active_probe=False,
        )
        pure_all_diagnostics = bridge.provider_diagnostics(
            provider="all",
            session_id="session-a",
            user_id="user-a",
            record=False,
            persist=False,
            active_probe=False,
        )
        pure_provider_status = bridge.provider_status(probe=False)
        expect(
            (
                len(reasoning.calls),
                len(fake.reads),
                fake.connection_checks,
            )
            == pure_read_counts,
            "pure-read summary, diagnostics, and status never invoke model or adapter",
            {
                "summary": pure_summary,
                "diagnostics": pure_diagnostics,
                "all_diagnostics": pure_all_diagnostics,
                "providers": pure_provider_status,
            },
        )
        expect(
            pure_summary["external_summary"]["status"] == "skipped"
            and pure_diagnostics["status"] == "probe_skipped"
            and pure_all_diagnostics["status"] == "probe_skipped",
            "pure-read surfaces name skipped active work",
            {
                "summary": pure_summary,
                "diagnostics": pure_diagnostics,
                "all_diagnostics": pure_all_diagnostics,
            },
        )
        expect(
            store.read_json("agent_memory.json") == before_diagnostics_state
            and store.read_jsonl("memory_log.jsonl") == before_diagnostics_log,
            "all pure-read memory surfaces leave state and logs unchanged",
        )

        before_unknown_state = store.read_json("agent_memory.json")
        before_unknown_log = store.read_jsonl("memory_log.jsonl")
        unknown = bridge.write_patch(
            {
                "user_id": "user-a",
                "session_id": "session-a",
                "summary": "must not persist",
            },
            provider="made-up",
        )
        expect(
            unknown.get("status") == "blocked"
            and unknown.get("reason") == "unknown_provider",
            "unknown provider fails closed",
            unknown,
        )
        expect(
            store.read_json("agent_memory.json") == before_unknown_state
            and store.read_jsonl("memory_log.jsonl") == before_unknown_log,
            "unknown provider cannot create local memory or audit persistence",
        )
        try:
            bridge.read_summary(
                "session-a",
                provider="made-up",
                user_id="user-a",
            )
        except ValueError as exc:
            expect("unknown memory provider" in str(exc), "unknown read provider is explicitly rejected", exc)
        else:
            raise AssertionError("unknown read provider was accepted")
        unknown_diagnostics = bridge.provider_diagnostics(
            provider="made-up",
            session_id="session-a",
            user_id="user-a",
            record=True,
            persist=True,
            active_probe=True,
        )
        expect(
            unknown_diagnostics.get("status") == "not_configured"
            and unknown_diagnostics.get("reason") == "unknown_provider",
            "unknown diagnostics provider is explicitly rejected",
            unknown_diagnostics,
        )
        expect(
            store.read_json("agent_memory.json") == before_unknown_state
            and store.read_jsonl("memory_log.jsonl") == before_unknown_log,
            "unknown read and diagnostics providers remain persistence-free",
        )

        blocked = bridge.write_patch({"session_id": "session-a", "summary": "missing owner"}, provider="local")
        expect(
            blocked.get("status") == "blocked"
            and blocked.get("reason") == "invalid_memory_scope",
            "memory write without owner fails closed",
            blocked,
        )

        unconfigured_openclaw = OpenClawAdapter(base_url="")
        openclaw_summary = unconfigured_openclaw.fetch_memory_summary("mem-session")
        openclaw_write = unconfigured_openclaw.write_memory_patch({"session_id": "mem-session", "summary": "x"})
        expect(openclaw_summary["status"] == "not_configured", "unconfigured OpenClaw memory summary is explicit", openclaw_summary)
        expect(openclaw_write["status"] == "not_configured", "unconfigured OpenClaw memory write is explicit", openclaw_write)

    print("memory quality smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
