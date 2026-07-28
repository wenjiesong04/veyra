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

from core.capability_registry import CapabilityRegistry  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.veyra_controller import VeyraController  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_contract import AGENT_CONTRACT_VERSION  # noqa: E402
from interface.event_schema import Decision, Route  # noqa: E402


REQUIRED_FEATURES = {
    "structured_task_packet": True,
    "rendered_prompt_fallback": True,
    "task_status": True,
    "stop_task": True,
}
OBSERVED_AT = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def configure_openclaw(store: WorldStateStore) -> None:
    store.write_json(
        "agent_config.json",
        {
            "selected_agent": "openclaw",
            "agents": {
                "openclaw": {
                    "kind": "openclaw",
                    "enabled": True,
                    "base_url": "ws://127.0.0.1:18789",
                    "capabilities": ["web_search", "vision"],
                }
            },
        },
    )


def exact_v2_capabilities(
    *,
    observed_at: datetime = OBSERVED_AT,
    governed_dispatch: bool = True,
    runtime: str = "openclaw",
    tools: list[str] | None = None,
) -> dict[str, Any]:
    raw = {
        "runtime": runtime,
        "status": "available",
        "connected": True,
        "contract_version": AGENT_CONTRACT_VERSION,
        "features": dict(REQUIRED_FEATURES),
        "tools": list(tools or ["web_search"]),
        "compatibility": {
            "status": "compatible",
            "native_adapter": False,
        },
    }
    raw["features"].update(
        {
            "tool_proxy_enforced": governed_dispatch,
            "tool_proxy_identity_match": governed_dispatch,
            "tool_proxy_enforcement_scope": (
                "veyra_governed_openclaw_sessions"
            ),
            "governance_callbacks_complete": governed_dispatch,
        }
    )
    return {
        **raw,
        "raw": dict(raw),
        "updated_at": observed_at.isoformat(),
        "ttl_seconds": 300,
    }


def agent_allowed_policy(
    *,
    preferred_route: Route = Route.ASK_USER,
) -> dict[str, Any]:
    return {
        "semantic_policy": {
            "schema": "veyra.semantic_policy.v1",
            "preferred_route": preferred_route.value,
            "requires_clarification": False,
            "allowed_capabilities": [],
            "allowed_effects": ["agent.execute"],
            "denied_effects": [],
        }
    }


def write_executor_observation(
    store: WorldStateStore,
    *,
    observed_at: datetime,
    runtime: str = "openclaw",
    tools: list[str] | None = None,
    governed_dispatch: bool = True,
) -> None:
    store.write_json(
        "executor_state.json",
        {
            "selected_agent": runtime,
            "status": "available",
            "connected": True,
            "capabilities": exact_v2_capabilities(
                observed_at=observed_at,
                governed_dispatch=governed_dispatch,
                runtime=runtime,
                tools=tools,
            ),
        },
    )


def check_config_only_fails_closed() -> None:
    with TemporaryDirectory(prefix="veyra-provider-config-only-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        configure_openclaw(store)
        snapshot = CapabilityRegistry(
            store,
            now=OBSERVED_AT,
        ).snapshot()
        capabilities = snapshot["capabilities"]
        expect(
            capabilities["selected_agent_runtime"]["available"] is False
            and capabilities["openclaw_runtime"]["available"] is False
            and capabilities["openclaw.web_search"]["available"] is False,
            "configuration and advertised config tokens do not certify a provider",
            capabilities,
        )
        expect(
            snapshot["provider_certification"]["validated"] is False,
            "config-only provider certification is unavailable",
            snapshot["provider_certification"],
        )


def check_fresh_exact_v2_is_available() -> None:
    with TemporaryDirectory(prefix="veyra-provider-fresh-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        configure_openclaw(store)
        store.write_json(
            "executor_state.json",
            {
                "selected_agent": "openclaw",
                "status": "available",
                "connected": True,
                "capabilities": exact_v2_capabilities(),
                "capability_snapshot": {
                    "runtime": "openclaw",
                    "updated_at": "2000-01-01T00:00:00+00:00",
                    "ttl_seconds": 1,
                    "tools": [],
                },
            },
        )
        snapshot = CapabilityRegistry(
            store,
            now=OBSERVED_AT + timedelta(seconds=1),
        ).snapshot()
        capabilities = snapshot["capabilities"]
        certification = snapshot["provider_certification"]
        expect(
            capabilities["selected_agent_runtime"]["available"] is True
            and capabilities["openclaw_runtime"]["available"] is True
            and capabilities["openclaw.web_search"]["available"] is True,
            "fresh exact-compatible v2 observation certifies runtime and capability",
            capabilities,
        )
        expect(
            certification["validated"] is True
            and certification["freshness"]["status"] == "fresh"
            and certification["evidence"]["contract"]["exact_match"] is True,
            "fresh provider certification records exact v2 evidence",
            certification,
        )
        expect(
            capabilities["openclaw.vision"]["available"] is False,
            "config-only capability token cannot borrow provider certification",
            capabilities["openclaw.vision"],
        )
        expect(
            capabilities["openclaw.web_search"]["source"]
            == "executor_state.capabilities",
            "top-level executor capabilities outrank legacy snapshot",
            capabilities["openclaw.web_search"],
        )


def check_stale_top_level_fails_closed() -> None:
    with TemporaryDirectory(prefix="veyra-provider-stale-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        configure_openclaw(store)
        legacy_fresh_at = OBSERVED_AT + timedelta(seconds=301)
        store.write_json(
            "executor_state.json",
            {
                "selected_agent": "openclaw",
                "status": "available",
                "connected": True,
                "ttl_seconds": 300,
                "capabilities": exact_v2_capabilities(),
                "capability_snapshot": {
                    "runtime": "openclaw",
                    "status": "available",
                    "connected": True,
                    "updated_at": legacy_fresh_at.isoformat(),
                    "ttl_seconds": 300,
                    "tools": ["web_search"],
                },
            },
        )
        snapshot = CapabilityRegistry(
            store,
            now=OBSERVED_AT + timedelta(seconds=301),
        ).snapshot()
        capabilities = snapshot["capabilities"]
        certification = snapshot["provider_certification"]
        expect(
            certification["certification_status"] == "stale"
            and certification["validated"] is False,
            "stale top-level observation invalidates certification",
            certification,
        )
        expect(
            capabilities["selected_agent_runtime"]["available"] is False
            and capabilities["openclaw_runtime"]["available"] is False
            and capabilities["openclaw.web_search"]["available"] is False,
            "fresh-looking legacy snapshot cannot rescue stale provider truth",
            capabilities,
        )


def check_certification_without_enforcement_is_diagnostic_only() -> None:
    with TemporaryDirectory(
        prefix="veyra-provider-diagnostic-only-"
    ) as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        configure_openclaw(store)
        store.write_json(
            "executor_state.json",
            {
                "selected_agent": "openclaw",
                "status": "available",
                "connected": True,
                "capabilities": exact_v2_capabilities(
                    governed_dispatch=False
                ),
            },
        )
        snapshot = CapabilityRegistry(
            store,
            now=OBSERVED_AT + timedelta(seconds=1),
        ).snapshot()
        capabilities = snapshot["capabilities"]
        expect(
            snapshot["provider_certification"]["validated"] is True
            and snapshot["selected_agent"]["provider_certified"] is True
            and snapshot["selected_agent"]["task_dispatch_available"]
            is False
            and snapshot["routes"]["agent"] is False,
            "protocol certification alone cannot enable Agent routing",
            snapshot,
        )
        expect(
            capabilities["selected_agent_runtime"]["available"] is False
            and capabilities["selected_agent_runtime"]["status"]
            == "diagnostic_only"
            and capabilities["openclaw.web_search"]["available"] is False,
            "missing local Tool Proxy enforcement keeps runtime capabilities diagnostic-only",
            capabilities,
        )


def check_controller_refreshes_agent_fallbacks() -> None:
    current = OBSERVED_AT + timedelta(seconds=301)
    with TemporaryDirectory(
        prefix="veyra-provider-controller-refresh-"
    ) as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        configure_openclaw(store)
        write_executor_observation(
            store,
            observed_at=OBSERVED_AT,
            tools=["web_search", "vision"],
        )
        refreshes: list[str] = []

        def refresh() -> None:
            refreshes.append("refresh")
            write_executor_observation(
                store,
                observed_at=current,
                tools=["web_search", "vision"],
            )

        controller = VeyraController(
            CapabilityRegistry(store, now=current),
            agent_runtime_refresher=refresh,
        )
        capability_gap, capability_plan = controller.prepare(
            Decision(
                route=Route.ASK_USER,
                risk_level=RiskLevel.R1,
                reason="image understanding needs a governed runtime",
                intent="information",
                required_capabilities=["vision"],
                model_assist=agent_allowed_policy(),
            )
        )
        expect(
            capability_gap.route == Route.AGENT
            and capability_plan.status == "rerouted"
            and "openclaw.vision"
            in capability_gap.required_capabilities
            and "controller:agent_capability_fallback"
            in capability_gap.signals
            and len(refreshes) == 1,
            "stale ASK_USER capability gap refreshes once before Agent fallback",
            {
                "decision": capability_gap.to_dict(),
                "plan": capability_plan.to_dict(),
                "refreshes": refreshes,
            },
        )

        write_executor_observation(
            store,
            observed_at=OBSERVED_AT,
            tools=["web_search", "vision"],
        )
        refreshes.clear()
        unsupported, unsupported_plan = controller.prepare(
            Decision(
                route=Route.PROBE,
                risk_level=RiskLevel.R1,
                reason="unsupported search needs governed evidence",
                intent="information",
                selected_probe="youtube_search",
                needs_probe=True,
                required_capabilities=["web_search"],
                model_assist=agent_allowed_policy(
                    preferred_route=Route.PROBE
                ),
            )
        )
        expect(
            unsupported.route == Route.AGENT
            and unsupported_plan.status == "rerouted"
            and "openclaw.web_search"
            in unsupported.required_capabilities
            and "controller:unsupported_probe_agent_fallback"
            in unsupported.signals
            and len(refreshes) == 1,
            "stale unsupported probe refreshes once before Agent fallback",
            {
                "decision": unsupported.to_dict(),
                "plan": unsupported_plan.to_dict(),
                "refreshes": refreshes,
            },
        )


def check_controller_refresh_failure_is_closed() -> None:
    current = OBSERVED_AT + timedelta(seconds=301)
    with TemporaryDirectory(
        prefix="veyra-provider-controller-failure-"
    ) as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        configure_openclaw(store)
        write_executor_observation(
            store,
            observed_at=OBSERVED_AT,
            tools=["web_search", "vision"],
        )
        refreshes: list[str] = []

        def fail_refresh() -> None:
            refreshes.append("failed")
            raise RuntimeError("simulated provider observation failure")

        controller = VeyraController(
            CapabilityRegistry(store, now=current),
            agent_runtime_refresher=fail_refresh,
        )
        capability_gap, capability_plan = controller.prepare(
            Decision(
                route=Route.ASK_USER,
                risk_level=RiskLevel.R1,
                reason="image understanding needs a governed runtime",
                intent="information",
                required_capabilities=["vision"],
                model_assist=agent_allowed_policy(),
            )
        )
        expect(
            capability_gap.route == Route.ASK_USER
            and capability_plan.status == "missing_capability"
            and len(refreshes) == 1,
            "failed capability-gap refresh remains ASK_USER",
            {
                "decision": capability_gap.to_dict(),
                "plan": capability_plan.to_dict(),
                "refreshes": refreshes,
            },
        )

        refreshes.clear()
        unsupported, unsupported_plan = controller.prepare(
            Decision(
                route=Route.PROBE,
                risk_level=RiskLevel.R1,
                reason="unsupported search needs governed evidence",
                intent="information",
                selected_probe="youtube_search",
                needs_probe=True,
                required_capabilities=["web_search"],
                model_assist=agent_allowed_policy(
                    preferred_route=Route.PROBE
                ),
            )
        )
        expect(
            unsupported.route == Route.ASK_USER
            and unsupported_plan.status == "missing_probe"
            and len(refreshes) == 1,
            "failed unsupported-probe refresh remains ASK_USER",
            {
                "decision": unsupported.to_dict(),
                "plan": unsupported_plan.to_dict(),
                "refreshes": refreshes,
            },
        )


def check_controller_refresh_scope_and_generic_lock() -> None:
    current = OBSERVED_AT + timedelta(seconds=301)
    with TemporaryDirectory(
        prefix="veyra-provider-controller-scope-"
    ) as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        configure_openclaw(store)
        write_executor_observation(
            store,
            observed_at=OBSERVED_AT,
            tools=["vision"],
        )
        refreshes: list[str] = []

        def refresh() -> None:
            refreshes.append("refresh")
            write_executor_observation(
                store,
                observed_at=current,
                tools=["vision"],
            )

        controller = VeyraController(
            CapabilityRegistry(store, now=current),
            agent_runtime_refresher=refresh,
        )
        explicit, explicit_plan = controller.prepare(
            Decision(
                route=Route.AGENT,
                risk_level=RiskLevel.R1,
                reason="explicit governed runtime request",
                intent="information",
                needs_agent=True,
                model_assist=agent_allowed_policy(),
            )
        )
        expect(
            explicit.route == Route.AGENT
            and explicit_plan.status == "ready"
            and len(refreshes) == 1,
            "explicit Agent route refreshes once without duplicate fallback probes",
            {
                "decision": explicit.to_dict(),
                "plan": explicit_plan.to_dict(),
                "refreshes": refreshes,
            },
        )

        refreshes.clear()
        write_executor_observation(
            store,
            observed_at=OBSERVED_AT,
            tools=["vision"],
        )
        denied = {
            "semantic_policy": {
                **agent_allowed_policy()["semantic_policy"],
                "allowed_effects": [],
                "denied_effects": ["agent.execute"],
            }
        }
        denied_decision, _ = controller.prepare(
            Decision(
                route=Route.ASK_USER,
                risk_level=RiskLevel.R1,
                reason="semantic policy denies provider use",
                intent="information",
                required_capabilities=["vision"],
                model_assist=denied,
            )
        )
        r2_decision, _ = controller.prepare(
            Decision(
                route=Route.ASK_USER,
                risk_level=RiskLevel.R2,
                reason="risk level denies automatic fallback",
                intent="information",
                required_capabilities=["vision"],
                model_assist=agent_allowed_policy(),
            )
        )
        expect(
            denied_decision.route == Route.ASK_USER
            and r2_decision.route == Route.ASK_USER
            and refreshes == [],
            "semantic denial and R2 capability gaps do not probe or fallback to Agent",
            {
                "denied": denied_decision.to_dict(),
                "r2": r2_decision.to_dict(),
                "refreshes": refreshes,
            },
        )

    with TemporaryDirectory(
        prefix="veyra-provider-controller-generic-"
    ) as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        store.write_json(
            "agent_config.json",
            {
                "selected_agent": "candidate-runtime",
                "agents": {
                    "candidate-runtime": {
                        "kind": "custom",
                        "enabled": True,
                        "base_url": "http://127.0.0.1:9876",
                    }
                },
            },
        )
        write_executor_observation(
            store,
            observed_at=OBSERVED_AT,
            runtime="candidate-runtime",
            tools=["vision"],
        )
        refreshes: list[str] = []

        def refresh_generic() -> None:
            refreshes.append("refresh")
            write_executor_observation(
                store,
                observed_at=current,
                runtime="candidate-runtime",
                tools=["vision"],
            )

        generic, generic_plan = VeyraController(
            CapabilityRegistry(store, now=current),
            agent_runtime_refresher=refresh_generic,
        ).prepare(
            Decision(
                route=Route.ASK_USER,
                risk_level=RiskLevel.R1,
                reason="generic provider cannot gain dispatch authority",
                intent="information",
                required_capabilities=["vision"],
                model_assist=agent_allowed_policy(),
            )
        )
        expect(
            generic.route == Route.ASK_USER
            and generic_plan.status == "missing_capability"
            and refreshes == ["refresh"],
            "fresh generic provider remains diagnostic-only after fallback refresh",
            {
                "decision": generic.to_dict(),
                "plan": generic_plan.to_dict(),
                "refreshes": refreshes,
            },
        )


def main() -> int:
    check_config_only_fails_closed()
    check_fresh_exact_v2_is_available()
    check_certification_without_enforcement_is_diagnostic_only()
    check_stale_top_level_fails_closed()
    check_controller_refreshes_agent_fallbacks()
    check_controller_refresh_failure_is_closed()
    check_controller_refresh_scope_and_generic_lock()
    print("capability registry provider truth smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
