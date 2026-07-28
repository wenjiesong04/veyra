#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from guardian.review_queue import ReviewQueue  # noqa: E402
from interface.agent_registry import AgentRegistry  # noqa: E402
from runtime.authority_fence import agent_transport_call_inflight  # noqa: E402
from runtime.proactive_checks import ProactiveChecks  # noqa: E402
from runtime.self_heal_playbook import (  # noqa: E402
    PLAYBOOK_ID,
    OpenClawReconnectPlaybook,
)
from runtime.tool_governance_runtime import (  # noqa: E402
    ToolGovernanceConflict,
    ToolGovernanceRuntime,
)
from tool_proxy.governance_contract import GovernedSessionBinding  # noqa: E402


PRIVATE_SENTINEL = "PRIVATE_SELF_HEAL_TOKEN_SENTINEL"


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail!r}")
    print(f"ok - {label}")


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 28, 1, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def capabilities(
    healthy: bool,
    *,
    private_raw: bool = False,
    server_version: str = "phase5-fixture",
) -> dict[str, Any]:
    if not healthy:
        return {
            "runtime": "openclaw",
            "status": "unavailable",
            "connected": False,
            "compatibility": {
                "status": "unavailable",
                "required_methods": {"health": False},
            },
            "features": {},
            "raw": {
                "health": {"ok": False},
                "error": (
                    f"token={PRIVATE_SENTINEL} /Users/private/gateway"
                    if private_raw
                    else "unavailable"
                ),
            },
        }
    return {
        "contract_version": "veyra.agent_adapter.v2",
        "runtime": "openclaw",
        "status": "available",
        "connected": True,
        "protocol": "openclaw_gateway_ws",
        "compatibility": {
            "status": "compatible",
            "required_methods": {
                "chat.send": True,
                "health": True,
                "status": True,
                "tools.catalog": True,
                "skills.status": True,
            },
        },
        "features": {
            "agent_dialogue_v1": True,
            "bounded_agent_dialogue": True,
            "caller_supplied_run_id": True,
            "idempotent_submit": True,
            "exact_stop": True,
            "enforced_execution_profile": "phase3_sandbox_proposal",
            "tool_proxy_enforced": True,
            "web_search": True,
        },
        "raw": {
            "health": {"ok": True},
            "server": {
                "method_count": 8,
                "protocol": "openclaw_gateway_ws",
                "version": server_version,
            },
            "gateway_status": {"tasks": {"active": 0}},
            "tools": {
                "items": [
                    {"id": "web_search"},
                    {"id": f"unsafe/{PRIVATE_SENTINEL}"},
                ]
            },
            "skills": {"items": [{"id": "bounded_analysis"}]},
            **(
                {"private": f"token={PRIVATE_SENTINEL}"}
                if private_raw
                else {}
            ),
        },
    }


def failed_observation(
    *,
    round_id: str,
    observed_at: str,
) -> dict[str, Any]:
    return {
        "qualifies": True,
        "round_id": round_id,
        "observed_at": observed_at,
        "sources": [
            {
                "source": "openclaw_tcp_probe",
                "status": "unavailable",
                "passed": False,
                "observed_at": observed_at,
                "round_id": round_id,
                "evidence_kind": "real_probe",
                "error_code": None,
            },
            {
                "source": "openclaw_gateway_protocol",
                "status": "unavailable",
                "passed": False,
                "observed_at": observed_at,
                "round_id": round_id,
                "evidence_kind": "real_force_refresh",
                "error_code": None,
                "active_task_count": None,
                "active_task_count_malformed": False,
                "capability_snapshot": None,
            },
        ],
    }


class ScriptedAdapter:
    def __init__(
        self,
        *,
        observations: list[dict[str, Any]],
        recoveries: list[dict[str, Any]] | None = None,
        raise_private: bool = False,
        gateway_url: str = "ws://127.0.0.1:18789",
    ) -> None:
        self.gateway_url = gateway_url
        self.observations = list(observations)
        self.recoveries = list(recoveries or [])
        self.raise_private = raise_private
        self.connection_calls = 0
        self.fetch_calls = 0
        self.invalidate_calls = 0

    @staticmethod
    def _next(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return capabilities(False)
        return items.pop(0) if len(items) > 1 else items[0]

    def connection_status(self, *, force_refresh: bool = False) -> dict[str, Any]:
        self.connection_calls += 1
        if self.raise_private:
            raise RuntimeError(
                f"token={PRIVATE_SENTINEL} /Users/private/openclaw"
            )
        caps = self._next(self.observations)
        return {
            "connected": bool(caps.get("connected")),
            "status": caps.get("status"),
            "capabilities": caps,
            "force_refresh_observed": force_refresh,
        }

    def invalidate_capabilities_cache(self) -> None:
        self.invalidate_calls += 1

    def fetch_capabilities(self, *, force_refresh: bool = False) -> dict[str, Any]:
        self.fetch_calls += 1
        if self.raise_private:
            raise RuntimeError(
                f"api_key={PRIVATE_SENTINEL} /Users/private/openclaw"
            )
        return self._next(self.recoveries)


class ScriptedProbe:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = list(statuses)
        self.calls = 0
        self.ports: list[int] = []

    def __call__(self, port: int) -> dict[str, Any]:
        self.calls += 1
        self.ports.append(port)
        status = (
            self.statuses.pop(0)
            if len(self.statuses) > 1
            else self.statuses[0]
        )
        return {
            "probe": "openclaw_probe",
            "source": "openclaw_probe",
            "target": "openclaw_runtime",
            "status": status,
            "validation": {
                "source": "real_probe",
                "observed": True,
                "validated": status == "listening",
            },
            "details": {"port": port},
        }


class BlockingConnectionAdapter(ScriptedAdapter):
    def __init__(self, *, started: Event, release: Event) -> None:
        super().__init__(observations=[capabilities(True)])
        self.started = started
        self.release = release

    def connection_status(self, *, force_refresh: bool = False) -> dict[str, Any]:
        self.connection_calls += 1
        self.started.set()
        if not self.release.wait(2.0):
            raise TimeoutError("test did not release connection observation")
        caps = self._next(self.observations)
        return {
            "connected": bool(caps.get("connected")),
            "status": caps.get("status"),
            "capabilities": caps,
            "force_refresh_observed": force_refresh,
        }


class BlockingRecoveryAdapter(ScriptedAdapter):
    def __init__(self, *, started: Event, release: Event) -> None:
        super().__init__(
            observations=[capabilities(False), capabilities(False)],
            recoveries=[capabilities(False)],
        )
        self.started = started
        self.release = release

    def fetch_capabilities(self, *, force_refresh: bool = False) -> dict[str, Any]:
        self.fetch_calls += 1
        self.started.set()
        if not self.release.wait(5.0):
            raise TimeoutError("test did not release recovery worker")
        return self._next(self.recoveries)


class StateReadingAdapter(ScriptedAdapter):
    def __init__(self, store: WorldStateStore) -> None:
        super().__init__(
            observations=[capabilities(False), capabilities(False)],
            recoveries=[capabilities(True)],
        )
        self.store = store

    def connection_status(self, *, force_refresh: bool = False) -> dict[str, Any]:
        self.store.read_json("tool_governance_state.json")
        return super().connection_status(force_refresh=force_refresh)

    def fetch_capabilities(self, *, force_refresh: bool = False) -> dict[str, Any]:
        self.store.read_json("openclaw_tool_hook_state.json")
        return super().fetch_capabilities(force_refresh=force_refresh)


class BlockingRefreshRegistry(AgentRegistry):
    def __init__(
        self,
        store: WorldStateStore,
        *,
        old_adapter: ScriptedAdapter,
        new_adapter: ScriptedAdapter,
        refresh_started: Event,
        release_refresh: Event,
    ) -> None:
        self.old_adapter = old_adapter
        self.new_adapter = new_adapter
        self.refresh_started = refresh_started
        self.release_refresh = release_refresh
        self.block_next_refresh = False
        super().__init__(store)

    def arm_refresh_block(self) -> None:
        self.block_next_refresh = True

    def refresh(self) -> None:
        if self.block_next_refresh:
            self.refresh_started.set()
            if not self.release_refresh.wait(2.0):
                raise TimeoutError("test did not release registry refresh")
            self.block_next_refresh = False
        super().refresh()

    def _build_adapter(
        self,
        name: str,
        config: dict[str, Any],
    ) -> Any:
        if name != "openclaw":
            return None
        return (
            self.new_adapter
            if config.get("protocol_min") == 7
            else self.old_adapter
        )

    def list_status(self) -> dict[str, Any]:
        return {
            "selected_agent": self.selected_name(),
            "agents": {
                name: {"status": "fixture"}
                for name in self._adapters
            },
        }


def governed_binding(run_id: str) -> GovernedSessionBinding:
    return GovernedSessionBinding.create(
        user_id="local-user",
        workspace_id="phase5-timeout-workspace",
        agent_id="openclaw",
        session_id=f"session-{run_id}",
        channel_id="api",
        case_id=f"case-{run_id}",
        step_id=f"step-{run_id}",
        run_id=run_id,
    )


def configure(
    store: WorldStateStore,
    *,
    mode: str,
    mode_epoch: int = 1,
    base_url: str = "ws://127.0.0.1:18789",
    enabled: bool = True,
) -> None:
    def update(config: dict[str, Any]) -> None:
        config["self_heal"] = {
            "openclaw_reconnect": {
                "mode": mode,
                "mode_epoch": mode_epoch,
                "allowed_modes": [
                    "disabled",
                    "record_only",
                    "shadow",
                    "scoped_canary",
                ],
            }
        }

    store.mutate_json("ops_config.json", update)
    store.mutate_json(
        "agent_config.json",
        lambda config: config.update(
            {
                "selected_agent": "openclaw",
                "agents": {
                    **(
                        config.get("agents")
                        if isinstance(config.get("agents"), dict)
                        else {}
                    ),
                    "openclaw": {
                        "kind": "openclaw",
                        "base_url": base_url,
                        "enabled": enabled,
                    },
                },
            }
        ),
    )


def configure_mode_only(
    store: WorldStateStore,
    *,
    mode: str,
    mode_epoch: int,
) -> None:
    def update(config: dict[str, Any]) -> None:
        config["self_heal"] = {
            "openclaw_reconnect": {
                "mode": mode,
                "mode_epoch": mode_epoch,
                "allowed_modes": [
                    "disabled",
                    "record_only",
                    "shadow",
                    "scoped_canary",
                ],
            }
        }

    store.mutate_json("ops_config.json", update)


def controller(
    store: WorldStateStore,
    adapter: Any,
    probe: Any,
    clock: FakeClock,
    *,
    review_creator: Any = None,
    call_timeout_seconds: float = 25.0,
) -> OpenClawReconnectPlaybook:
    return OpenClawReconnectPlaybook(
        state_store=store,
        adapter_resolver=lambda: adapter,
        review_creator=review_creator,
        probe_runner=probe,
        now=clock,
        call_timeout_seconds=call_timeout_seconds,
    )


def test_shadow_and_healthy(root: Path) -> None:
    store = WorldStateStore(root / "shadow-state")
    configure(store, mode="shadow")
    clock = FakeClock()
    down_adapter = ScriptedAdapter(observations=[capabilities(False)])
    down_probe = ScriptedProbe(["closed"])
    playbook = controller(store, down_adapter, down_probe, clock)
    shadow = playbook.run()
    expect(
        shadow["status"] == "shadow_qualified"
        and shadow["attempt_count"] == 0
        and shadow["failure_confirmation_count"] == 0
        and down_adapter.fetch_calls == 0,
        "default shadow observes a candidate without acting",
        shadow,
    )

    configure(store, mode="scoped_canary", mode_epoch=2)
    healthy_adapter = ScriptedAdapter(observations=[capabilities(True)])
    healthy_probe = ScriptedProbe(["listening"])
    healthy = controller(store, healthy_adapter, healthy_probe, clock).run()
    executor = store.read_json("executor_state.json")
    expect(
        healthy["status"] == "healthy"
        and healthy["attempt_count"] == 0
        and healthy_adapter.fetch_calls == 0,
        "fresh healthy dual verification is a no-op",
        healthy,
    )
    snapshot = executor.get("capability_snapshot") or {}
    expect(
        snapshot.get("freshness") == "fresh"
        and snapshot.get("health", {}).get("ok") is True
        and PRIVATE_SENTINEL
        not in json.dumps(snapshot, ensure_ascii=False),
        "only a fresh safe capability projection is persisted",
        snapshot,
    )


def test_recovery_and_partial_verifier(root: Path) -> None:
    store = WorldStateStore(root / "recovery-state")
    configure(store, mode="scoped_canary")
    clock = FakeClock()
    adapter = ScriptedAdapter(
        observations=[capabilities(False), capabilities(False)],
        recoveries=[capabilities(True, private_raw=True)],
    )
    probe = ScriptedProbe(["closed", "closed", "listening"])
    playbook = controller(store, adapter, probe, clock)
    first = playbook.run()
    expect(
        first["status"] == "confirming_failure"
        and first["failure_confirmation_count"] == 1
        and first["attempt_count"] == 0,
        "one fresh dual-source failure only confirms the incident",
        first,
    )
    clock.advance(1)
    recovered = playbook.run()
    expect(
        recovered["status"] == "recovered"
        and recovered["last_verification"]["passed"] is True
        and adapter.fetch_calls == 1
        and adapter.invalidate_calls == 1,
        "second failure admits one cache-bypass handshake and dual verifier",
        recovered,
    )
    public = json.dumps(
        {
            "result": recovered,
            "state": store.read_json("self_heal_state.json"),
            "executor": store.read_json("executor_state.json"),
        },
        ensure_ascii=False,
    )
    expect(
        PRIVATE_SENTINEL not in public and "/Users/private" not in public,
        "fresh recovery projections omit private raw Gateway data",
    )

    partial_store = WorldStateStore(root / "partial-state")
    configure(partial_store, mode="scoped_canary")
    partial_clock = FakeClock()
    partial_adapter = ScriptedAdapter(
        observations=[capabilities(False), capabilities(False)],
        recoveries=[capabilities(True)],
    )
    partial_probe = ScriptedProbe(["closed", "closed", "closed"])
    partial = controller(
        partial_store,
        partial_adapter,
        partial_probe,
        partial_clock,
    )
    partial.run()
    partial_clock.advance(1)
    result = partial.run()
    expect(
        result["status"] == "cooldown"
        and result["last_verification"]["passed"] is False
        and partial_store.read_json("executor_state.json").get("connected")
        is False,
        "capability success without TCP success cannot claim recovery",
        result,
    )


def test_cooldown_breaker_review_and_concurrency(root: Path) -> None:
    store = WorldStateStore(root / "breaker-state")
    configure(store, mode="scoped_canary")
    clock = FakeClock()
    adapter = ScriptedAdapter(
        observations=[
            capabilities(False, private_raw=True),
            capabilities(False, private_raw=True),
            capabilities(False, private_raw=True),
        ],
        recoveries=[
            capabilities(False, private_raw=True),
            capabilities(False, private_raw=True),
        ],
    )
    probe = ScriptedProbe(["closed", "closed", "closed", "closed", "closed"])
    queue = ReviewQueue(store)
    proactive = ProactiveChecks(
        store,
        agency_root=str(root / "breaker-agency"),
        model_assist_enabled=False,
        review_queue=queue,
        agent_adapter_resolver=lambda: adapter,
    )
    proactive.self_heal = controller(
        store,
        adapter,
        probe,
        clock,
        review_creator=proactive._create_self_heal_restart_review,
    )

    first = proactive.self_heal.run()
    clock.advance(1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        first_attempt_results = list(
            pool.map(lambda _: proactive.self_heal.run(), range(8))
        )
    cooldown = proactive.self_heal.status()
    expect(
        first["failure_confirmation_count"] == 1
        and cooldown["status"] == "cooldown"
        and cooldown["attempt_count"] == 1
        and adapter.fetch_calls == 1,
        "concurrent confirmation produces one first attempt",
        first_attempt_results,
    )
    before = (
        adapter.connection_calls,
        adapter.fetch_calls,
        probe.calls,
    )
    clock.advance(299)
    repeated = proactive.self_heal.run()
    after = (
        adapter.connection_calls,
        adapter.fetch_calls,
        probe.calls,
    )
    expect(
        repeated["status"] == "cooldown" and before == after,
        "299-second retry is suppressed without new network work",
        {"before": before, "after": after, "result": repeated},
    )

    clock.advance(1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        second_attempt_results = list(
            pool.map(lambda _: proactive.self_heal.run(), range(8))
        )
    opened = proactive.self_heal.status()
    reviews = [
        item
        for item in queue.list()
        if (item.get("proposal") or {}).get("playbook_id") == PLAYBOOK_ID
    ]
    expect(
        opened["status"] == "breaker_open"
        and opened["attempt_count"] == 2
        and opened["effective_autonomy_level"] == "A1"
        and adapter.fetch_calls == 2,
        "second failed attempt opens the circuit and degrades autonomy",
        second_attempt_results,
    )
    expect(
        len(reviews) == 1
        and reviews[0].get("risk_level") == "R4"
        and (reviews[0].get("proposal") or {}).get("type")
        == "manual_agent_restart_review"
        and (reviews[0].get("proposal") or {}).get(
            "execution_authority_enabled"
        )
        is False,
        "breaker creates one non-executable R4 restart review",
        reviews,
    )
    unsupported = proactive.execute_approved_proposal(
        reviews[0].get("proposal") or {}
    )
    expect(
        unsupported.get("status") == "governance_only"
        and unsupported.get("method") == "none",
        "Phase 5 fallback review records approval without restart authority",
        unsupported,
    )
    counts_before = (
        adapter.connection_calls,
        adapter.fetch_calls,
        probe.calls,
    )
    proactive.self_heal.run()
    expect(
        len(
            [
                item
                for item in queue.list()
                if (item.get("proposal") or {}).get("playbook_id")
                == PLAYBOOK_ID
            ]
        )
        == 1
        and counts_before
        == (adapter.connection_calls, adapter.fetch_calls, probe.calls),
        "open circuit suppresses more handshakes and duplicate reviews",
    )
    clock.advance(300)
    observation_before = (
        adapter.connection_calls,
        adapter.fetch_calls,
        probe.calls,
    )
    observed_open = proactive.self_heal.run()
    observation_after = (
        adapter.connection_calls,
        adapter.fetch_calls,
        probe.calls,
    )
    expect(
        observed_open["status"] == "breaker_open"
        and observed_open["attempt_count"] == 2
        and observation_after[0] == observation_before[0] + 1
        and observation_after[1] == observation_before[1]
        and observation_after[2] == observation_before[2] + 1
        and len(
            [
                item
                for item in queue.list()
                if (item.get("proposal") or {}).get("playbook_id")
                == PLAYBOOK_ID
            ]
        )
        == 1,
        "open circuit permits only scheduled L1 observation",
        {
            "before": observation_before,
            "after": observation_after,
            "result": observed_open,
        },
    )
    public = json.dumps(
        {
            "status": opened,
            "reviews": reviews,
            "state": store.read_json("self_heal_state.json"),
        },
        ensure_ascii=False,
    )
    expect(
        PRIVATE_SENTINEL not in public and "/Users/private" not in public,
        "errors and reviews expose no private Gateway payload",
    )


def test_activity_scope_crash_and_corruption(root: Path) -> None:
    store = WorldStateStore(root / "safety-state")
    configure(store, mode="scoped_canary")
    clock = FakeClock()
    adapter = ScriptedAdapter(
        observations=[capabilities(False), capabilities(False)],
        recoveries=[capabilities(False)],
    )
    probe = ScriptedProbe(["closed", "closed", "closed"])
    playbook = controller(store, adapter, probe, clock)
    playbook.run()
    store.mutate_json(
        "openclaw_tool_hook_state.json",
        lambda state: state.setdefault("dispatches", {}).update(
            {"run-active": {"status": "active"}}
        ),
    )
    clock.advance(1)
    blocked = playbook.run()
    expect(
        blocked["status"] == "blocked_active_effects"
        and blocked["attempt_count"] == 0
        and adapter.fetch_calls == 0,
        "active governed side effect blocks transport refresh without budget use",
        blocked,
    )

    store.mutate_json(
        "openclaw_tool_hook_state.json",
        lambda state: state.setdefault("dispatches", {}).clear(),
    )
    clock.advance(1)
    attempted = playbook.run()
    expect(
        attempted["status"] == "cooldown"
        and attempted["attempt_count"] == 1,
        "cleared activity permits the already-confirmed incident",
        attempted,
    )

    crash_store = WorldStateStore(root / "crash-state")
    configure(crash_store, mode="scoped_canary")
    crash_clock = FakeClock()
    crash_adapter = ScriptedAdapter(observations=[capabilities(False)])
    crash_probe = ScriptedProbe(["closed"])
    crash_queue = ReviewQueue(crash_store)
    crash_pc = ProactiveChecks(
        crash_store,
        agency_root=str(root / "crash-agency"),
        model_assist_enabled=False,
        review_queue=crash_queue,
        agent_adapter_resolver=lambda: crash_adapter,
    )
    crash_pc.self_heal = controller(
        crash_store,
        crash_adapter,
        crash_probe,
        crash_clock,
        review_creator=crash_pc._create_self_heal_restart_review,
    )
    crash_pc.self_heal.run()
    crash_clock.advance(92)

    def inject_claim(state: dict[str, Any]) -> None:
        record = state["playbooks"][PLAYBOOK_ID]
        existing = list(record.get("failure_confirmations") or [])
        second_confirmation = failed_observation(
            round_id="obs_crash_second",
            observed_at=crash_clock().isoformat(),
        )
        record.update(
            {
                "status": "attempt_in_progress",
                "incident_id": "incident-crash",
                "attempt_count": 1,
                "failure_confirmation_count": 2,
                "failure_confirmations": [
                    existing[0],
                    second_confirmation,
                ],
                "last_observation": second_confirmation,
                "operation": {
                    "operation_id": "operation-crash",
                    "state": "claimed",
                    "attempt_number": 1,
                    "claimed_at": (
                        crash_clock() - timedelta(seconds=91)
                    ).isoformat(),
                },
            }
        )

    crash_store.mutate_json("self_heal_state.json", inject_claim)
    indeterminate = crash_pc.self_heal.run()
    expect(
        indeterminate["status"] == "indeterminate"
        and indeterminate["breaker_open"] is True
        and crash_adapter.fetch_calls == 0
        and len(crash_queue.list()) == 1,
        "expired attempt claim becomes indeterminate without replay",
        indeterminate,
    )

    provider_store = WorldStateStore(root / "provider-state")
    configure(provider_store, mode="scoped_canary")
    provider_store.mutate_json(
        "agent_config.json",
        lambda config: config.update({"selected_agent": "hermes"}),
    )
    provider_adapter = ScriptedAdapter(observations=[capabilities(False)])
    provider_probe = ScriptedProbe(["closed"])
    not_applicable = controller(
        provider_store,
        provider_adapter,
        provider_probe,
        FakeClock(),
    ).run()
    expect(
        not_applicable["status"] == "not_applicable"
        and provider_adapter.connection_calls == 0
        and provider_probe.calls == 0,
        "non-OpenClaw selected provider is never touched",
        not_applicable,
    )

    corrupt_store = WorldStateStore(root / "corrupt-state")
    configure(corrupt_store, mode="scoped_canary")
    corrupt_store.write_json(
        "self_heal_state.json",
        {
            "schema_version": "veyra.self_heal_state.v1",
            "playbooks": {
                PLAYBOOK_ID: {
                    "attempt_count": 99,
                    "failure_confirmation_count": 0,
                    "breaker_open": False,
                }
            },
        },
    )
    corrupt_adapter = ScriptedAdapter(observations=[capabilities(False)])
    corrupt_probe = ScriptedProbe(["closed"])
    fault = controller(
        corrupt_store,
        corrupt_adapter,
        corrupt_probe,
        FakeClock(),
    ).run()
    expect(
        fault["status"] == "fault"
        and fault["effective_autonomy_level"] == "A0"
        and corrupt_adapter.connection_calls == 0
        and corrupt_probe.calls == 0,
        "semantic state corruption freezes recovery at A0",
        fault,
    )


def test_mode_transition_preserves_claimed_operation(root: Path) -> None:
    store = WorldStateStore(root / "claimed-mode-transition-state")
    configure(store, mode="scoped_canary", mode_epoch=1)
    clock = FakeClock()
    adapter = ScriptedAdapter(
        observations=[
            capabilities(False),
            capabilities(False),
            capabilities(False),
        ],
        recoveries=[capabilities(True)],
    )
    probe = ScriptedProbe(
        ["closed", "closed", "closed", "listening"]
    )
    playbook = controller(store, adapter, probe, clock)
    first = playbook.run()
    clock.advance(1)

    def inject_live_claim(state: dict[str, Any]) -> None:
        record = state["playbooks"][PLAYBOOK_ID]
        first_confirmation = list(
            record.get("failure_confirmations") or []
        )[0]
        second_confirmation = failed_observation(
            round_id="obs_mode_transition_second",
            observed_at=clock().isoformat(),
        )
        record.update(
            {
                "status": "attempt_in_progress",
                "incident_id": "incident-mode-transition",
                "attempt_count": 1,
                "failure_confirmation_count": 2,
                "failure_confirmations": [
                    first_confirmation,
                    second_confirmation,
                ],
                "last_observation": second_confirmation,
                "operation": {
                    "operation_id": "operation-mode-transition",
                    "state": "claimed",
                    "attempt_number": 1,
                    "claimed_at": clock().isoformat(),
                },
            }
        )

    store.mutate_json("self_heal_state.json", inject_live_claim)
    configure_mode_only(store, mode="record_only", mode_epoch=2)
    record_only = playbook.run()
    configure_mode_only(store, mode="scoped_canary", mode_epoch=3)
    canary_first = playbook.run()
    clock.advance(1)
    canary_second = playbook.run()
    expect(
        first["failure_confirmation_count"] == 1
        and record_only["status"] == "indeterminate"
        and record_only["operation_state"] == "indeterminate"
        and record_only["attempt_count"] == 1
        and record_only["breaker_open"] is True
        and canary_first["breaker_open"] is True
        and canary_first["attempt_count"] == 1
        and canary_second["breaker_open"] is True
        and canary_second["attempt_count"] == 1
        and adapter.connection_calls == 1
        and adapter.fetch_calls == 0
        and adapter.invalidate_calls == 0
        and probe.calls == 1,
        (
            "mode changes retain an unresolved durable claim and cannot "
            "replay it"
        ),
        {
            "record_only": record_only,
            "canary_first": canary_first,
            "canary_second": canary_second,
            "connection_calls": adapter.connection_calls,
            "fetch_calls": adapter.fetch_calls,
            "probe_calls": probe.calls,
        },
    )


def test_registry_refresh_and_identity_scope_fail_closed(root: Path) -> None:
    registry_store = WorldStateStore(root / "registry-refresh-race-state")
    configure(registry_store, mode="scoped_canary")
    registry_store.write_json(
        "executor_state.json",
        {
            "status": "registry-race-sentinel",
            "connected": False,
            "capability_snapshot": {
                "freshness": "registry-race-sentinel"
            },
        },
    )
    old_adapter = ScriptedAdapter(
        observations=[
            capabilities(True, server_version="runtime-old-adapter")
        ]
    )
    new_adapter = ScriptedAdapter(
        observations=[
            capabilities(True, server_version="runtime-new-adapter")
        ]
    )
    refresh_started = Event()
    release_refresh = Event()
    registry = BlockingRefreshRegistry(
        registry_store,
        old_adapter=old_adapter,
        new_adapter=new_adapter,
        refresh_started=refresh_started,
        release_refresh=release_refresh,
    )
    registry.arm_refresh_block()
    registry_probe = ScriptedProbe(["listening"])
    registry_playbook = OpenClawReconnectPlaybook(
        state_store=registry_store,
        adapter_resolver=registry.selected,
        probe_runner=registry_probe,
        now=FakeClock(),
    )
    upsert_holder: dict[str, Any] = {}
    upsert_completed = Event()

    def run_upsert() -> None:
        try:
            upsert_holder["result"] = registry.upsert(
                "openclaw",
                {"protocol_min": 7},
            )
        except Exception as exc:
            upsert_holder["error"] = type(exc).__name__
        finally:
            upsert_completed.set()

    upsert_thread = Thread(
        target=run_upsert,
        name="phase5-registry-upsert",
    )
    upsert_thread.start()
    refresh_blocked = refresh_started.wait(1.0)
    config_during_refresh = (
        registry_store.read_json("agent_config.json")
        .get("agents", {})
        .get("openclaw", {})
    )
    old_adapter_still_bound = (
        registry._adapters.get("openclaw") is old_adapter
    )
    executor_before_registry_run = registry_store.read_json(
        "executor_state.json"
    )
    self_heal_holder: dict[str, Any] = {}
    self_heal_started = Event()
    self_heal_completed = Event()

    def run_registry_self_heal() -> None:
        self_heal_started.set()
        try:
            self_heal_holder["result"] = registry_playbook.run()
        except Exception as exc:
            self_heal_holder["error"] = type(exc).__name__
        finally:
            self_heal_completed.set()

    self_heal_thread = Thread(
        target=run_registry_self_heal,
        name="phase5-registry-self-heal",
    )
    self_heal_thread.start()
    run_started = self_heal_started.wait(1.0)
    run_blocked = not self_heal_completed.wait(0.05)
    executor_during_registry_gap = registry_store.read_json(
        "executor_state.json"
    )
    upsert_blocked = not upsert_completed.is_set()
    old_calls_during_gap = old_adapter.connection_calls
    new_calls_during_gap = new_adapter.connection_calls
    probe_calls_during_gap = registry_probe.calls
    release_refresh.set()
    upsert_thread.join(2.0)
    self_heal_thread.join(2.0)
    executor_after_registry_run = registry_store.read_json(
        "executor_state.json"
    )
    registry_result = self_heal_holder.get("result") or {}
    projected_server = (
        executor_after_registry_run.get("capability_snapshot", {})
        .get("server", {})
        .get("version")
    )
    expect(
        refresh_blocked
        and config_during_refresh.get("protocol_min") == 7
        and old_adapter_still_bound
        and upsert_blocked
        and run_started
        and run_blocked
        and executor_during_registry_gap == executor_before_registry_run
        and old_calls_during_gap == 0
        and new_calls_during_gap == 0
        and probe_calls_during_gap == 0
        and not upsert_thread.is_alive()
        and not self_heal_thread.is_alive()
        and "error" not in upsert_holder
        and "error" not in self_heal_holder
        and registry._adapters.get("openclaw") is new_adapter
        and registry_result.get("status") == "healthy"
        and old_adapter.connection_calls == 0
        and new_adapter.connection_calls == 1
        and registry_probe.calls == 1
        and projected_server == "runtime-new-adapter",
        (
            "AgentRegistry upsert binds config and adapter refresh before "
            "self-heal can observe either"
        ),
        {
            "config_during_refresh": config_during_refresh,
            "old_adapter_still_bound": old_adapter_still_bound,
            "upsert_blocked": upsert_blocked,
            "run_blocked": run_blocked,
            "upsert": upsert_holder,
            "self_heal": self_heal_holder,
            "old_calls": old_adapter.connection_calls,
            "new_calls": new_adapter.connection_calls,
            "probe_calls": registry_probe.calls,
            "executor_before": executor_before_registry_run,
            "executor_after": executor_after_registry_run,
        },
    )

    invalid_store = WorldStateStore(root / "missing-identity-scope-state")
    configure(invalid_store, mode="scoped_canary")
    bootstrap_adapter = ScriptedAdapter(
        observations=[
            capabilities(True, server_version="runtime-v1")
        ]
    )
    bootstrap_probe = ScriptedProbe(["listening"])
    bootstrap = controller(
        invalid_store,
        bootstrap_adapter,
        bootstrap_probe,
        FakeClock(),
    ).run()
    seeded_record = invalid_store.read_json(
        "self_heal_state.json"
    )["playbooks"][PLAYBOOK_ID]

    def remove_identity_scope(state: dict[str, Any]) -> None:
        state["playbooks"][PLAYBOOK_ID].pop(
            "identity_scope_digest",
            None,
        )

    invalid_store.mutate_json(
        "self_heal_state.json",
        remove_identity_scope,
    )
    invalid_executor_before = invalid_store.read_json("executor_state.json")
    invalid_adapter = ScriptedAdapter(
        observations=[
            capabilities(True, server_version="runtime-v1")
        ]
    )
    invalid_probe = ScriptedProbe(["listening"])
    invalid = controller(
        invalid_store,
        invalid_adapter,
        invalid_probe,
        FakeClock(),
    ).run()
    invalid_executor_after = invalid_store.read_json("executor_state.json")
    expect(
        bootstrap["status"] == "healthy"
        and bool(seeded_record.get("runtime_identity_digest"))
        and bool(seeded_record.get("identity_scope_digest"))
        and invalid["status"] == "fault"
        and invalid["fault_code"] == "invalid_state"
        and invalid["effective_autonomy_level"] == "A0"
        and invalid["automatic_effects"] == []
        and invalid_adapter.connection_calls == 0
        and invalid_adapter.fetch_calls == 0
        and invalid_adapter.invalidate_calls == 0
        and invalid_probe.calls == 0
        and invalid_executor_after == invalid_executor_before,
        (
            "runtime identity without its identity scope fails closed before "
            "all calls"
        ),
        invalid,
    )


def test_target_and_durable_state_fail_closed(root: Path) -> None:
    mismatch_store = WorldStateStore(root / "scope-mismatch-state")
    configure(mismatch_store, mode="scoped_canary")
    mismatch_adapter = ScriptedAdapter(
        observations=[capabilities(True)],
        gateway_url="ws://127.0.0.1:18790",
    )
    mismatch_probe = ScriptedProbe(["listening"])
    mismatch_executor_before = mismatch_store.read_json("executor_state.json")
    mismatch = controller(
        mismatch_store,
        mismatch_adapter,
        mismatch_probe,
        FakeClock(),
    ).run()
    expect(
        mismatch["status"] == "scope_mismatch"
        and mismatch["effective_autonomy_level"] == "A0"
        and mismatch["automatic_effects"] == []
        and mismatch_adapter.connection_calls == 0
        and mismatch_adapter.fetch_calls == 0
        and mismatch_adapter.invalidate_calls == 0
        and mismatch_probe.calls == 0
        and mismatch_store.read_json("executor_state.json")
        == mismatch_executor_before,
        "adapter/config scope mismatch is A0 with zero effects and calls",
        mismatch,
    )

    disabled_store = WorldStateStore(root / "disabled-target-state")
    configure(
        disabled_store,
        mode="scoped_canary",
        enabled=False,
    )
    disabled_adapter = ScriptedAdapter(observations=[capabilities(True)])
    disabled_probe = ScriptedProbe(["listening"])
    disabled_executor_before = disabled_store.read_json("executor_state.json")
    disabled_playbook = controller(
        disabled_store,
        disabled_adapter,
        disabled_probe,
        FakeClock(),
    )
    disabled_port = disabled_playbook.configured_port()
    disabled = disabled_playbook.run()
    expect(
        disabled["status"] == "disabled_target"
        and disabled["effective_autonomy_level"] == "A0"
        and disabled["automatic_effects"] == []
        and disabled_port is None
        and disabled_adapter.connection_calls == 0
        and disabled_adapter.fetch_calls == 0
        and disabled_adapter.invalidate_calls == 0
        and disabled_probe.calls == 0
        and disabled_store.read_json("executor_state.json")
        == disabled_executor_before,
        "disabled selected target performs zero transport or probe calls",
        {"result": disabled, "configured_port": disabled_port},
    )

    ops_store = WorldStateStore(root / "corrupt-ops-state")
    configure(ops_store, mode="scoped_canary")
    ops_store.path_for("ops_config.json").write_text(
        "{broken-ops-config",
        encoding="utf-8",
    )
    ops_adapter = ScriptedAdapter(observations=[capabilities(True)])
    ops_probe = ScriptedProbe(["listening"])
    ops_fault = controller(
        ops_store,
        ops_adapter,
        ops_probe,
        FakeClock(),
    ).run()
    expect(
        ops_fault["status"] == "fault"
        and ops_fault["effective_autonomy_level"] == "A0"
        and ops_fault["automatic_effects"] == []
        and ops_adapter.connection_calls == 0
        and ops_adapter.fetch_calls == 0
        and ops_probe.calls == 0,
        "corrupt ops config faults at A0 before any call",
        ops_fault,
    )

    operation_store = WorldStateStore(root / "unknown-operation-state")
    configure(operation_store, mode="scoped_canary")
    operation_clock = FakeClock()
    operation_adapter = ScriptedAdapter(observations=[capabilities(True)])
    operation_probe = ScriptedProbe(["listening"])
    operation_playbook = controller(
        operation_store,
        operation_adapter,
        operation_probe,
        operation_clock,
    )

    def inject_unknown_operation(state: dict[str, Any]) -> None:
        record = operation_playbook._new_record(  # noqa: SLF001
            target_binding_digest="a" * 64,
            status="attempt_in_progress",
            mode="scoped_canary",
            mode_epoch=1,
        )
        record.update(
            {
                "attempt_count": 1,
                "operation": {
                    "operation_id": "healop_unknown_state",
                    "state": "future_operation_state",
                    "attempt_number": 1,
                    "claimed_at": operation_clock().isoformat(),
                },
            }
        )
        state["playbooks"] = {PLAYBOOK_ID: record}

    operation_store.mutate_json(
        "self_heal_state.json",
        inject_unknown_operation,
    )
    operation_fault = operation_playbook.run()
    expect(
        operation_fault["status"] == "fault"
        and operation_fault["effective_autonomy_level"] == "A0"
        and operation_adapter.connection_calls == 0
        and operation_adapter.fetch_calls == 0
        and operation_probe.calls == 0,
        "unknown durable operation state fails closed before calls",
        operation_fault,
    )

    evidence_store = WorldStateStore(root / "incomplete-confirmation-state")
    configure(evidence_store, mode="scoped_canary")
    evidence_clock = FakeClock()
    evidence_adapter = ScriptedAdapter(observations=[capabilities(False)])
    evidence_probe = ScriptedProbe(["closed"])
    evidence_playbook = controller(
        evidence_store,
        evidence_adapter,
        evidence_probe,
        evidence_clock,
    )

    def inject_incomplete_confirmation(state: dict[str, Any]) -> None:
        incomplete = failed_observation(
            round_id="obs_incomplete",
            observed_at=evidence_clock().isoformat(),
        )
        incomplete["sources"] = incomplete["sources"][:1]
        record = evidence_playbook._new_record(  # noqa: SLF001
            target_binding_digest="b" * 64,
            status="confirming_failure",
            mode="scoped_canary",
            mode_epoch=1,
        )
        record.update(
            {
                "failure_confirmation_count": 1,
                "failure_confirmations": [incomplete],
                "last_observation": incomplete,
            }
        )
        state["playbooks"] = {PLAYBOOK_ID: record}

    evidence_store.mutate_json(
        "self_heal_state.json",
        inject_incomplete_confirmation,
    )
    evidence_fault = evidence_playbook.run()
    expect(
        evidence_fault["status"] == "fault"
        and evidence_fault["effective_autonomy_level"] == "A0"
        and evidence_adapter.connection_calls == 0
        and evidence_adapter.fetch_calls == 0
        and evidence_probe.calls == 0,
        "incomplete durable failure evidence faults before calls",
        evidence_fault,
    )


def test_ledger_unknowns_and_nonconsecutive_failures(root: Path) -> None:
    def unknown_dispatch(store: WorldStateStore) -> None:
        store.mutate_json(
            "openclaw_tool_hook_state.json",
            lambda state: state.setdefault("dispatches", {}).update(
                {"dispatch-future": {"status": "future_dispatch_state"}}
            ),
        )

    def unknown_attempt(store: WorldStateStore) -> None:
        store.mutate_json(
            "openclaw_tool_hook_state.json",
            lambda state: state.setdefault("attempts", {}).update(
                {"attempt-future": {"status": "future_attempt_state"}}
            ),
        )

    def unknown_session(store: WorldStateStore) -> None:
        store.mutate_json(
            "tool_governance_state.json",
            lambda state: state.setdefault("sessions", {}).update(
                {"session-future": {"status": "future_session_state"}}
            ),
        )

    def arbitrary_pending_task(store: WorldStateStore) -> None:
        store.mutate_json(
            "task_state.json",
            lambda state: state.update(
                {
                    "pending_agent_tasks": [
                        {
                            "task_id": "terminal-looking-but-still-pending",
                            "status": "verified_success",
                            "verification_status": "verified_success",
                        }
                    ]
                }
            ),
        )

    cases = (
        ("dispatch", unknown_dispatch),
        ("attempt", unknown_attempt),
        ("session", unknown_session),
        ("pending_task", arbitrary_pending_task),
    )
    for label, inject in cases:
        store = WorldStateStore(root / f"ledger-{label}-state")
        configure(store, mode="scoped_canary")
        clock = FakeClock()
        adapter = ScriptedAdapter(
            observations=[capabilities(False), capabilities(False)],
            recoveries=[capabilities(False)],
        )
        probe = ScriptedProbe(["closed", "closed"])
        playbook = controller(store, adapter, probe, clock)
        first = playbook.run()
        inject(store)
        clock.advance(1)
        blocked = playbook.run()
        expect(
            first["failure_confirmation_count"] == 1
            and blocked["status"] == "blocked_active_effects"
            and blocked["attempt_count"] == 0
            and adapter.fetch_calls == 0
            and adapter.invalidate_calls == 0,
            f"unknown {label} ledger state blocks reconnect",
            blocked,
        )

    reset_store = WorldStateStore(root / "nonconsecutive-state")
    configure(reset_store, mode="scoped_canary")
    reset_clock = FakeClock()
    reset_adapter = ScriptedAdapter(
        observations=[
            capabilities(False),
            capabilities(False),
            capabilities(False),
        ],
        recoveries=[capabilities(False)],
    )
    reset_probe = ScriptedProbe(["closed", "listening", "closed"])
    reset_playbook = controller(
        reset_store,
        reset_adapter,
        reset_probe,
        reset_clock,
    )
    first = reset_playbook.run()
    reset_clock.advance(1)
    partial = reset_playbook.run()
    reset_clock.advance(1)
    third = reset_playbook.run()
    expect(
        first["failure_confirmation_count"] == 1
        and partial["status"] == "observing"
        and partial["failure_confirmation_count"] == 0
        and third["status"] == "confirming_failure"
        and third["failure_confirmation_count"] == 1
        and third["attempt_count"] == 0
        and reset_adapter.fetch_calls == 0,
        "a partial middle round resets non-consecutive failure confirmation",
        {"first": first, "partial": partial, "third": third},
    )

    expired_store = WorldStateStore(root / "expired-confirmation-state")
    configure(expired_store, mode="scoped_canary")
    expired_clock = FakeClock()
    expired_adapter = ScriptedAdapter(
        observations=[capabilities(False), capabilities(False)],
        recoveries=[capabilities(True)],
    )
    expired_probe = ScriptedProbe(["closed", "closed"])
    expired_playbook = controller(
        expired_store,
        expired_adapter,
        expired_probe,
        expired_clock,
    )
    expired_first = expired_playbook.run()
    expired_clock.advance(361)
    expired_second = expired_playbook.run()
    expect(
        expired_first["failure_confirmation_count"] == 1
        and expired_second["status"] == "confirming_failure"
        and expired_second["failure_confirmation_count"] == 1
        and expired_second["attempt_count"] == 0
        and expired_adapter.connection_calls == 2
        and expired_adapter.fetch_calls == 0
        and expired_adapter.invalidate_calls == 0,
        "a failure beyond the 360-second window starts a new confirmation chain",
        {"first": expired_first, "second": expired_second},
    )


def test_identity_timeout_races_state_reads_and_port(root: Path) -> None:
    healthy_identity_store = WorldStateStore(
        root / "healthy-identity-drift-state"
    )
    configure(healthy_identity_store, mode="scoped_canary")
    healthy_identity_clock = FakeClock()
    healthy_identity_adapter = ScriptedAdapter(
        observations=[
            capabilities(True, server_version="runtime-v1"),
            capabilities(True, server_version="runtime-v2"),
        ],
    )
    healthy_identity_probe = ScriptedProbe(["listening", "listening"])
    healthy_identity_playbook = controller(
        healthy_identity_store,
        healthy_identity_adapter,
        healthy_identity_probe,
        healthy_identity_clock,
    )
    healthy_v1 = healthy_identity_playbook.run()
    executor_before_healthy_drift = healthy_identity_store.read_json(
        "executor_state.json"
    )
    healthy_identity_clock.advance(1)
    healthy_v2 = healthy_identity_playbook.run()
    executor_after_healthy_drift = healthy_identity_store.read_json(
        "executor_state.json"
    )
    expect(
        healthy_v1["status"] == "healthy"
        and healthy_v1["runtime_identity_status"] == "established"
        and healthy_v2["status"] == "identity_mismatch"
        and healthy_v2["runtime_identity_status"] == "mismatch"
        and healthy_v2["breaker_open"] is True
        and healthy_v2["attempt_count"] == 0
        and healthy_identity_adapter.connection_calls == 2
        and healthy_identity_adapter.fetch_calls == 0
        and healthy_identity_probe.calls == 2
        and executor_after_healthy_drift == executor_before_healthy_drift,
        (
            "healthy runtime identity drift opens the breaker without "
            "projection"
        ),
        {
            "first": healthy_v1,
            "second": healthy_v2,
            "executor_before": executor_before_healthy_drift,
            "executor_after": executor_after_healthy_drift,
        },
    )

    identity_store = WorldStateStore(root / "identity-drift-state")
    configure(identity_store, mode="scoped_canary")
    identity_clock = FakeClock()
    identity_adapter = ScriptedAdapter(
        observations=[
            capabilities(True, server_version="runtime-v1"),
            capabilities(False),
            capabilities(False),
        ],
        recoveries=[
            capabilities(True, server_version="runtime-v2"),
        ],
    )
    identity_probe = ScriptedProbe(
        ["listening", "closed", "closed", "listening"]
    )
    identity_playbook = controller(
        identity_store,
        identity_adapter,
        identity_probe,
        identity_clock,
    )
    established = identity_playbook.run()
    identity_clock.advance(1)
    first_failure = identity_playbook.run()
    executor_before_mismatch = identity_store.read_json("executor_state.json")
    identity_clock.advance(1)
    mismatch = identity_playbook.run()
    executor_after_mismatch = identity_store.read_json("executor_state.json")
    expect(
        established["status"] == "healthy"
        and established["runtime_identity_status"] == "established"
        and first_failure["failure_confirmation_count"] == 1
        and mismatch["status"] == "identity_mismatch"
        and mismatch["breaker_open"] is True
        and mismatch["attempt_count"] == 1
        and mismatch["last_verification"]["runtime_identity_status"]
        == "mismatch"
        and executor_after_mismatch == executor_before_mismatch,
        "runtime identity v1 to v2 opens the breaker without projection",
        {
            "result": mismatch,
            "executor_before": executor_before_mismatch,
            "executor_after": executor_after_mismatch,
        },
    )

    timeout_store = WorldStateStore(root / "timeout-fence-state")
    configure(timeout_store, mode="scoped_canary")
    timeout_store.write_json(
        "executor_state.json",
        {
            "status": "timeout-sentinel",
            "connected": False,
            "capability_snapshot": {"freshness": "timeout-sentinel"},
        },
    )
    executor_before_timeout = timeout_store.read_json("executor_state.json")
    ops_before_revocation = timeout_store.read_json("ops_config.json")
    timeout_clock = FakeClock()
    worker_started = Event()
    release_worker = Event()
    timeout_adapter = BlockingRecoveryAdapter(
        started=worker_started,
        release=release_worker,
    )
    timeout_probe = ScriptedProbe(["closed", "closed", "closed"])
    timeout_playbook = controller(
        timeout_store,
        timeout_adapter,
        timeout_probe,
        timeout_clock,
        call_timeout_seconds=0.05,
    )
    timeout_playbook.run()
    timeout_clock.advance(1)
    timed_out = timeout_playbook.run()
    configure_mode_only(
        timeout_store,
        mode="disabled",
        mode_epoch=2,
    )
    ops_after_revocation = timeout_store.read_json("ops_config.json")
    inflight_after_revocation = agent_transport_call_inflight(timeout_store)
    executor_before_late_exit = timeout_store.read_json("executor_state.json")
    governance = ToolGovernanceRuntime(timeout_store)
    binding = governed_binding("run-timeout-fence")
    rejected = False
    rejection = ""
    try:
        governance.register_session(binding)
    except ToolGovernanceConflict as exc:
        rejected = True
        rejection = str(exc)
    finally:
        release_worker.set()
    deadline = monotonic() + 2.0
    while (
        agent_transport_call_inflight(timeout_store)
        and monotonic() < deadline
    ):
        sleep(0.01)
    inflight_cleared = not agent_transport_call_inflight(timeout_store)
    executor_after_late_exit = timeout_store.read_json("executor_state.json")
    registered = governance.register_session(binding)
    expect(
        timed_out["status"] == "indeterminate"
        and timed_out["breaker_open"] is True
        and worker_started.is_set()
        and ops_after_revocation["_state_revision"]
        > ops_before_revocation["_state_revision"]
        and (
            ops_after_revocation.get("self_heal", {})
            .get("openclaw_reconnect", {})
            .get("mode")
            == "disabled"
        )
        and inflight_after_revocation
        and rejected
        and "in flight" in rejection
        and inflight_cleared
        and registered == binding
        and executor_before_late_exit == executor_before_timeout
        and executor_after_late_exit == executor_before_timeout,
        (
            "timeout permits config revocation but blocks session admission "
            "and late projection until worker exit"
        ),
        {
            "result": timed_out,
            "ops_before": ops_before_revocation,
            "ops_after": ops_after_revocation,
            "inflight_after_revocation": inflight_after_revocation,
            "rejected": rejected,
            "rejection": rejection,
            "inflight_cleared": inflight_cleared,
            "executor_before": executor_before_timeout,
            "executor_after": executor_after_late_exit,
        },
    )

    race_store = WorldStateStore(root / "mode-race-state")
    configure(race_store, mode="scoped_canary")
    race_store.write_json(
        "executor_state.json",
        {
            "status": "sentinel",
            "connected": False,
            "capability_snapshot": {"freshness": "sentinel"},
        },
    )
    executor_before_race = race_store.read_json("executor_state.json")
    connection_started = Event()
    release_connection = Event()
    race_adapter = BlockingConnectionAdapter(
        started=connection_started,
        release=release_connection,
    )
    race_probe = ScriptedProbe(["listening"])
    race_playbook = controller(
        race_store,
        race_adapter,
        race_probe,
        FakeClock(),
        call_timeout_seconds=1.0,
    )
    holder: dict[str, Any] = {}
    mode_write_started = Event()
    mode_write_completed = Event()
    mode_write_holder: dict[str, Any] = {}

    def run_race() -> None:
        holder["result"] = race_playbook.run()

    def write_mode_race() -> None:
        mode_write_started.set()
        try:
            configure_mode_only(
                race_store,
                mode="disabled",
                mode_epoch=2,
            )
        except Exception as exc:
            mode_write_holder["error"] = type(exc).__name__
        finally:
            mode_write_completed.set()

    race_thread = Thread(target=run_race, name="phase5-mode-race")
    race_thread.start()
    started = connection_started.wait(1.0)
    mode_write_thread = Thread(
        target=write_mode_race,
        name="phase5-mode-race-writer",
    )
    mode_write_thread.start()
    writer_started = mode_write_started.wait(1.0)
    writer_blocked = not mode_write_completed.wait(0.05)
    mode_while_blocked = (
        race_store.read_json("ops_config.json")
        .get("self_heal", {})
        .get("openclaw_reconnect", {})
        .get("mode")
    )
    executor_while_blocked = race_store.read_json("executor_state.json")
    release_connection.set()
    race_thread.join(2.0)
    mode_write_thread.join(2.0)
    executor_after_race = race_store.read_json("executor_state.json")
    final_mode = (
        race_store.read_json("ops_config.json")
        .get("self_heal", {})
        .get("openclaw_reconnect", {})
        .get("mode")
    )
    sleep(0.05)
    executor_after_settle = race_store.read_json("executor_state.json")
    disabled_after_race = race_playbook.run()
    executor_after_disabled = race_store.read_json("executor_state.json")
    race_result = holder.get("result") or {}
    expect(
        started
        and writer_started
        and writer_blocked
        and mode_while_blocked == "scoped_canary"
        and not race_thread.is_alive()
        and not mode_write_thread.is_alive()
        and not mode_write_holder
        and race_result.get("status") == "healthy"
        and race_result.get("mode") == "scoped_canary"
        and final_mode == "disabled"
        and disabled_after_race.get("status") == "disabled"
        and race_adapter.fetch_calls == 0
        and executor_while_blocked == executor_before_race
        and executor_after_race == executor_after_settle
        and executor_after_disabled == executor_after_race,
        (
            "mode writes wait for the run authority fence and cannot cause "
            "late projection"
        ),
        {
            "result": race_result,
            "writer_blocked": writer_blocked,
            "mode_while_blocked": mode_while_blocked,
            "write_error": mode_write_holder,
            "final_mode": final_mode,
            "disabled_result": disabled_after_race,
            "run_thread_alive": race_thread.is_alive(),
            "write_thread_alive": mode_write_thread.is_alive(),
            "executor_before": executor_before_race,
            "executor_after": executor_after_race,
        },
    )

    config_race_store = WorldStateStore(root / "config-race-state")
    configure(config_race_store, mode="scoped_canary")
    config_race_store.write_json(
        "executor_state.json",
        {
            "status": "sentinel",
            "connected": True,
            "capability_snapshot": {"freshness": "sentinel"},
        },
    )
    config_clock = FakeClock()
    recovery_started = Event()
    release_recovery = Event()
    config_adapter = BlockingRecoveryAdapter(
        started=recovery_started,
        release=release_recovery,
    )
    config_adapter.observations = [
        capabilities(False),
        capabilities(False),
        capabilities(True, server_version="runtime-v1"),
    ]
    config_adapter.recoveries = [
        capabilities(True, server_version="runtime-v1")
    ]
    config_probe = ScriptedProbe(
        ["closed", "closed", "listening", "listening"]
    )
    config_playbook = controller(
        config_race_store,
        config_adapter,
        config_probe,
        config_clock,
        call_timeout_seconds=1.0,
    )
    first_config_failure = config_playbook.run()
    config_clock.advance(1)
    executor_before_config_race = config_race_store.read_json(
        "executor_state.json"
    )
    config_holder: dict[str, Any] = {}
    config_write_started = Event()
    config_write_completed = Event()
    config_write_holder: dict[str, Any] = {}

    def run_config_race() -> None:
        config_holder["result"] = config_playbook.run()

    def write_config_race() -> None:
        config_write_started.set()
        try:
            config_race_store.mutate_json(
                "agent_config.json",
                lambda config: config["agents"]["openclaw"].update(
                    {"protocol_min": 7}
                ),
            )
        except Exception as exc:
            config_write_holder["error"] = type(exc).__name__
        finally:
            config_write_completed.set()

    config_thread = Thread(
        target=run_config_race,
        name="phase5-config-race",
    )
    config_thread.start()
    config_started = recovery_started.wait(1.0)
    config_write_thread = Thread(
        target=write_config_race,
        name="phase5-config-race-writer",
    )
    config_write_thread.start()
    config_writer_started = config_write_started.wait(1.0)
    config_writer_blocked = not config_write_completed.wait(0.05)
    selected_while_config_blocked = (
        config_race_store.read_json("agent_config.json")
        .get("agents", {})
        .get("openclaw", {})
    )
    executor_while_config_blocked = config_race_store.read_json(
        "executor_state.json"
    )
    release_recovery.set()
    config_thread.join(2.0)
    config_write_thread.join(2.0)
    executor_after_config_race = config_race_store.read_json(
        "executor_state.json"
    )
    selected_after_config = (
        config_race_store.read_json("agent_config.json")
        .get("agents", {})
        .get("openclaw", {})
    )
    sleep(0.05)
    executor_after_config_settle = config_race_store.read_json(
        "executor_state.json"
    )
    state_before_scope_rebind = config_race_store.read_json(
        "self_heal_state.json"
    )["playbooks"][PLAYBOOK_ID]
    config_clock.advance(1)
    post_config = config_playbook.run()
    state_after_scope_rebind = config_race_store.read_json(
        "self_heal_state.json"
    )["playbooks"][PLAYBOOK_ID]
    config_race_result = config_holder.get("result") or {}
    expect(
        first_config_failure["failure_confirmation_count"] == 1
        and config_started
        and config_writer_started
        and config_writer_blocked
        and selected_while_config_blocked.get("protocol_min") != 7
        and not config_thread.is_alive()
        and not config_write_thread.is_alive()
        and not config_write_holder
        and config_race_result.get("status") == "recovered"
        and config_race_result.get("mode") == "scoped_canary"
        and selected_after_config.get("protocol_min") == 7
        and config_adapter.fetch_calls == 1
        and executor_while_config_blocked == executor_before_config_race
        and executor_after_config_race == executor_after_config_settle
        and post_config["status"] == "healthy"
        and state_after_scope_rebind["target_binding_digest"]
        != state_before_scope_rebind["target_binding_digest"]
        and state_after_scope_rebind["identity_scope_digest"]
        == state_before_scope_rebind["identity_scope_digest"]
        and state_after_scope_rebind["runtime_identity_digest"]
        == state_before_scope_rebind["runtime_identity_digest"]
        and bool(state_after_scope_rebind["runtime_identity_digest"]),
        (
            "non-endpoint config writes serialize after the run, change "
            "action scope, and preserve runtime identity scope"
        ),
        {
            "result": config_race_result,
            "post_config": post_config,
            "writer_blocked": config_writer_blocked,
            "selected_while_blocked": selected_while_config_blocked,
            "write_error": config_write_holder,
            "run_thread_alive": config_thread.is_alive(),
            "write_thread_alive": config_write_thread.is_alive(),
            "selected_config": selected_after_config,
            "executor_before": executor_before_config_race,
            "executor_after": executor_after_config_race,
            "state_before": state_before_scope_rebind,
            "state_after": state_after_scope_rebind,
        },
    )

    reading_store = WorldStateStore(root / "state-reading-adapter-state")
    configure(reading_store, mode="scoped_canary")
    reading_clock = FakeClock()
    reading_adapter = StateReadingAdapter(reading_store)
    reading_probe = ScriptedProbe(["closed", "closed", "listening"])
    reading_playbook = controller(
        reading_store,
        reading_adapter,
        reading_probe,
        reading_clock,
        call_timeout_seconds=0.5,
    )
    reading_playbook.run()
    reading_clock.advance(1)
    read_started = monotonic()
    read_result = reading_playbook.run()
    read_duration = monotonic() - read_started
    expect(
        read_result["status"] == "recovered"
        and reading_adapter.fetch_calls == 1
        and read_duration < 1.0,
        "adapter state reads complete without authority-fence deadlock",
        {"result": read_result, "duration": read_duration},
    )

    custom_store = WorldStateStore(root / "custom-port-state")
    custom_port = 24567
    custom_url = f"ws://127.0.0.1:{custom_port}/gateway"
    configure(
        custom_store,
        mode="shadow",
        base_url=custom_url,
    )
    custom_adapter = ScriptedAdapter(
        observations=[capabilities(False)],
        gateway_url=custom_url,
    )
    custom_probe = ScriptedProbe(["closed"])
    custom_playbook = controller(
        custom_store,
        custom_adapter,
        custom_probe,
        FakeClock(),
    )
    configured_port = custom_playbook.configured_port()
    custom_result = custom_playbook.run()
    expect(
        configured_port == custom_port
        and custom_result["status"] == "shadow_qualified"
        and custom_probe.ports == [custom_port],
        "self-heal uses the exact configured custom OpenClaw port",
        {
            "configured_port": configured_port,
            "probe_ports": custom_probe.ports,
            "result": custom_result,
        },
    )


def main() -> None:
    with TemporaryDirectory(prefix="veyra-phase5-self-heal-") as tmp:
        root = Path(tmp)
        test_shadow_and_healthy(root)
        test_recovery_and_partial_verifier(root)
        test_cooldown_breaker_review_and_concurrency(root)
        test_activity_scope_crash_and_corruption(root)
        test_mode_transition_preserves_claimed_operation(root)
        test_registry_refresh_and_identity_scope_fail_closed(root)
        test_target_and_durable_state_fail_closed(root)
        test_ledger_unknowns_and_nonconsecutive_failures(root)
        test_identity_timeout_races_state_reads_and_port(root)
    print("openclaw_reconnect_playbook_smoke: ok")


if __name__ == "__main__":
    main()
