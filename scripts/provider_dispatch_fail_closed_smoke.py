#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop
from core.commitment_core import CommitmentCore
from core.runtime_entity import RuntimeEntity
from core.world_state import WorldStateStore
from interface.agent_adapter import UnavailableAgentAdapter
from interface.agent_contract import AGENT_CONTRACT_VERSION
from interface.agent_registry import AgentRegistry
from interface.event_normalizer import EventNormalizer
from interface.event_schema import VeyraTaskPacket
from interface.http_agent_adapter import AgentHttpConfig, HttpAgentAdapter
from runtime.runtime_matrix import RuntimeMatrix


FEATURES = {
    "structured_task_packet": True,
    "rendered_prompt_fallback": True,
    "task_status": True,
    "stop_task": True,
}


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


class RecordingAdapter(HttpAgentAdapter):
    def __init__(self, capabilities: dict[str, Any]) -> None:
        super().__init__(
            AgentHttpConfig(
                name="candidate-runtime",
                base_url="http://127.0.0.1:9876",
            )
        )
        self.capabilities = capabilities
        self.calls: list[tuple[str, str]] = []

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del payload
        self.calls.append((method, path))
        if method == "GET" and path == self.config.capabilities_path:
            return dict(self.capabilities)
        if method == "POST" and path == self.config.task_path:
            return {
                "task_id": "task_provider_certification",
                "status": "success",
                "result": "read-only analysis complete",
            }
        if method == "GET" and path.startswith(
            f"{self.config.memory_summary_path}?"
        ):
            return {
                "status": "success",
                "summary": "certified bounded summary",
            }
        if method == "GET" and path == self.config.status_path_template.format(
            task_id="task_provider_certification"
        ):
            return {
                "task_id": "task_provider_certification",
                "status": "success",
                "result": "certified task complete",
            }
        if method == "POST" and path == self.config.memory_patch_path:
            return {"status": "submitted"}
        if method == "POST" and path == self.config.stop_path_template.format(
            task_id="task_provider_certification"
        ):
            return {"status": "success", "stopped": True}
        raise AssertionError(f"unexpected request: {method} {path}")


def packet() -> VeyraTaskPacket:
    return VeyraTaskPacket(
        task_id="task_provider_certification",
        target_agent="candidate-runtime",
        session_id="provider-certification-smoke",
        user_message="Analyze the supplied context without side effects.",
        user_goal="Analyze the supplied context without side effects.",
        required_capabilities=[],
        context_patch={},
        persona_patch={},
        policy_patch={},
    )


def capability_document(
    *,
    features: dict[str, Any],
) -> dict[str, Any]:
    return {
        "runtime": "candidate-runtime",
        "status": "available",
        "connected": True,
        "contract_version": AGENT_CONTRACT_VERSION,
        "features": features,
        "compatibility": {"status": "compatible"},
    }


class StaticRegistry:
    def __init__(self, adapter: RecordingAdapter) -> None:
        self.adapter = adapter

    def get(self, name: str) -> RecordingAdapter:
        expect(
            name == "candidate-runtime",
            "runtime matrix requests exact provider identity",
            name,
        )
        return self.adapter


class RecordingMemoryBridge:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def provider_diagnostics(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {"status": "success", "summary": {"status": "success"}}


def main() -> int:
    unverified = RecordingAdapter(
        capability_document(
            features={
                "rendered_prompt_fallback": True,
            }
        )
    )
    connection = unverified.connection_status()
    before = len(
        [call for call in unverified.calls if call[0] == "POST"]
    )
    blocked = unverified.send_task(packet())
    summary_blocked = unverified.fetch_memory_summary(
        "private-session-id"
    )
    status_blocked = unverified.fetch_task_status(
        "task_provider_certification"
    )
    memory_blocked = unverified.write_memory_patch(
        {"summary": "must not dispatch"}
    )
    stop_blocked = unverified.stop_task(
        "task_provider_certification"
    )
    after = len(
        [call for call in unverified.calls if call[0] == "POST"]
    )
    expect(
        connection["validation"]["validated"] is False
        and connection["provider_certification"]["validated"] is False
        and blocked.status == "blocked"
        and blocked.raw.get("dispatch_allowed") is False
        and summary_blocked.get("status") == "blocked"
        and summary_blocked.get("dispatch_allowed") is False
        and status_blocked.status == "blocked"
        and status_blocked.raw.get("dispatch_allowed") is False
        and isinstance(memory_blocked, dict)
        and memory_blocked.get("status") == "blocked"
        and memory_blocked.get("dispatch_allowed") is False
        and stop_blocked is False
        and before == after == 0,
        "unverified provider cannot dispatch tasks, status, memory, or stop calls",
        {
            "connection": connection,
            "execution": blocked.to_dict(),
            "calls": unverified.calls,
        },
    )
    expect(
        all(
            path == unverified.config.capabilities_path
            for _method, path in unverified.calls
        ),
        "unverified provider receives capability probes only",
        unverified.calls,
    )

    matrix_adapter = RecordingAdapter(
        capability_document(
            features={
                "rendered_prompt_fallback": True,
            }
        )
    )
    matrix_memory = RecordingMemoryBridge()
    with TemporaryDirectory(
        prefix="veyra-provider-matrix-"
    ) as matrix_root:
        matrix = RuntimeMatrix(
            WorldStateStore(Path(matrix_root) / "state"),
            StaticRegistry(matrix_adapter),  # type: ignore[arg-type]
            matrix_memory,  # type: ignore[arg-type]
        )
        matrix_row = matrix._runtime_row(
            "candidate-runtime",
            write_memory_probe=True,
        )
    expect(
        matrix_row["provider_certification"]["validated"] is False
        and matrix_row["memory"]["status"] == "blocked"
        and matrix_row["task_status"]["status"] == "blocked"
        and matrix_memory.calls == []
        and all(
            path == matrix_adapter.config.capabilities_path
            for _method, path in matrix_adapter.calls
        ),
        "runtime matrix short-circuits all probes after failed certification",
        {
            "row": matrix_row,
            "adapter_calls": matrix_adapter.calls,
            "memory_calls": matrix_memory.calls,
        },
    )

    certified = RecordingAdapter(
        capability_document(features=dict(FEATURES))
    )
    ready = certified.connection_status()
    executed = certified.send_task(packet())
    summary_read = certified.fetch_memory_summary(
        "certified-session"
    )
    status_read = certified.fetch_task_status(
        "task_provider_certification"
    )
    memory_written = certified.write_memory_patch(
        {"summary": "certified bounded patch"}
    )
    stopped = certified.stop_task(
        "task_provider_certification"
    )
    expect(
        ready["validation"]["validated"] is True
        and ready["provider_certification"]["validated"] is True
        and executed.status == "blocked"
        and executed.raw.get("reason")
        == "generic_provider_dispatch_not_certified"
        and summary_read.get("status") == "success"
        and status_read.status == "success"
        and isinstance(memory_written, dict)
        and memory_written.get("status") == "blocked"
        and memory_written.get("dispatch_allowed") is False
        and memory_written.get("reason")
        == "generic_provider_memory_write_not_certified"
        and stopped is False
        and len(
            [call for call in certified.calls if call[0] == "POST"]
        )
        == 0,
        "fresh exact v2 generic provider remains read-only diagnostics without dispatch authority",
        {
            "connection": ready,
            "execution": executed.to_dict(),
            "calls": certified.calls,
        },
    )

    with TemporaryDirectory(prefix="veyra-agent-registry-") as raw_root:
        store = WorldStateStore(Path(raw_root) / "state")
        config = store.read_json("agent_config.json")
        config["selected_agent"] = "missing-runtime"
        store.write_json("agent_config.json", config)
        registry = AgentRegistry(store)
        expect(
            registry.selected_name() == "missing-runtime",
            "configured selected identity is not rewritten to OpenClaw",
            registry.selected_name(),
        )
        unavailable = registry.selected()
        unavailable_status = unavailable.connection_status()
        unavailable_result = unavailable.send_task(packet())
        expect(
            isinstance(unavailable, UnavailableAgentAdapter)
            and unavailable_status["name"] == "missing-runtime"
            and unavailable_status["connected"] is False
            and unavailable_status["dispatch_allowed"] is False
            and unavailable_result.executor == "missing-runtime"
            and unavailable_result.status == "adapter_unconfigured"
            and unavailable_result.raw.get("dispatch_allowed") is False,
            "unknown selected runtime exposes a side-effect-free unavailable adapter",
            {
                "status": unavailable_status,
                "execution": unavailable_result.to_dict(),
            },
        )
        try:
            registry.get("missing-runtime")
        except KeyError:
            pass
        else:
            raise AssertionError(
                "explicit unknown runtime lookup must remain an error"
            )
        expect(
            "missing-runtime" not in registry.names()
            and registry.list_status()["selected_status"]["name"]
            == "missing-runtime",
            "unavailable selection is reported without entering provider discovery",
            registry.list_status(),
        )

        disabled_config = store.read_json("agent_config.json")
        disabled_config["selected_agent"] = "disabled-runtime"
        disabled_config.setdefault("agents", {})["disabled-runtime"] = {
            "kind": "custom",
            "enabled": False,
            "base_url": "http://127.0.0.1:9999",
        }
        store.write_json("agent_config.json", disabled_config)
        registry.refresh()
        disabled = registry.selected()
        expect(
            isinstance(disabled, UnavailableAgentAdapter)
            and disabled.connection_status()["name"]
            == "disabled-runtime"
            and disabled.connection_status()["reason"]
            == "selected_runtime_disabled"
            and "disabled-runtime" not in registry.names(),
            "disabled selected runtime stays selected and fail-closed",
            registry.list_status(),
        )

        loop = AwarenessLoop(
            store,
            RuntimeEntity(store),
            commitment_core=CommitmentCore(store),
        )
        normalizer = EventNormalizer()
        direct = loop.handle_event(
            normalizer.user_message(
                "你是 Veyra 还是其他 Agent？",
                "api",
                "provider-smoke-user",
                "provider-smoke-direct",
            )
        ).to_dict()
        probe = loop.handle_event(
            normalizer.user_message(
                "现在东京时间是几点？",
                "api",
                "provider-smoke-user",
                "provider-smoke-probe",
            )
        ).to_dict()
        expect(
            direct["route"] == "direct_answer"
            and direct["status"] == "success"
            and probe["route"] == "probe"
            and probe["status"] in {"success", "verified_success"},
            "unavailable selected provider does not disable direct or local probe routes",
            {"direct": direct, "probe": probe},
        )

    print("provider dispatch fail-closed smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
