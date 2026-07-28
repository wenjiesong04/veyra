#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
from fastapi import FastAPI


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agency_core import AgencyCore  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from routers.debug_audit import build_debug_audit_router  # noqa: E402
from runtime.proactive_checks import ProactiveChecks  # noqa: E402


PRIVATE_SENTINEL = "PRIVATE_HTTP_SELF_HEAL_SENTINEL"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail!r}")
    print(f"ok - {label}")


class PrivateCapabilityAdapter:
    gateway_url = "ws://127.0.0.1:18789"

    def __init__(self) -> None:
        self.connection_calls = 0

    def connection_status(self, *, force_refresh: bool = False) -> dict[str, Any]:
        self.connection_calls += 1
        return {
            "connected": True,
            "status": "available",
            "capabilities": {
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
                    "enforced_execution_profile": (
                        "phase3_sandbox_proposal"
                    ),
                    "tool_proxy_enforced": True,
                },
                "raw": {
                    "health": {"ok": True},
                    "server": {
                        "method_count": 8,
                        "protocol": "openclaw_gateway_ws",
                        "version": "private-http-fixture",
                    },
                    "gateway_status": {"tasks": {"active": 0}},
                    "tools": {"items": [{"id": "web_search"}]},
                    "skills": {"items": [{"id": "bounded_analysis"}]},
                    "error": (
                        f"token={PRIVATE_SENTINEL} "
                        "/Users/private/openclaw"
                    ),
                },
            },
            "force_refresh_observed": force_refresh,
        }


def probe(
    name: str,
    *,
    status: str = "ok",
    source: str | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    return {
        "probe": f"{name}_probe",
        "source": source or f"{name}_probe",
        "target": target or name,
        "status": status,
        "summary": f"{name} {status}",
        "confidence": 0.9,
        "ttl_seconds": 30,
        "details": {},
        "validation": {
            "source": "real_probe",
            "observed": True,
            "validated": status in {"ok", "listening"},
        },
    }


def compact_verifier_contract(
    projection: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    source_items: list[dict[str, Any]] = []
    for field in ("last_observation", "last_verification"):
        evidence = projection.get(field)
        if not isinstance(evidence, dict):
            continue
        sources = evidence.get("sources")
        if isinstance(sources, list):
            source_items.extend(
                item for item in sources if isinstance(item, dict)
            )

    required_source_keys = {
        "source",
        "status",
        "passed",
        "observed_at",
    }
    allowed_source_keys = {
        *required_source_keys,
        "active_task_count",
        "active_task_count_malformed",
        "evidence_kind",
    }
    invalid_sources: list[dict[str, Any]] = []
    observed_sources: set[str] = set()
    for item in source_items:
        source = str(item.get("source") or "")
        observed_sources.add(source)
        if (
            not required_source_keys.issubset(item)
            or not set(item).issubset(allowed_source_keys)
            or not isinstance(item.get("status"), str)
            or not item.get("status")
            or type(item.get("passed")) is not bool
            or not isinstance(item.get("observed_at"), str)
            or not item.get("observed_at")
            or (
                source == "openclaw_gateway_protocol"
                and item.get("active_task_count") != 0
            )
            or (
                "active_task_count_malformed" in item
                and type(item.get("active_task_count_malformed")) is not bool
            )
            or (
                "evidence_kind" in item
                and item.get("evidence_kind")
                not in {"real_probe", "real_force_refresh"}
            )
        ):
            invalid_sources.append(item)

    forbidden_keys: list[str] = []
    private_values: list[str] = []

    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower()
                if (
                    normalized
                    in {
                        "capability_snapshot",
                        "round_id",
                        "server",
                        "skills",
                        "tools",
                    }
                    or "digest" in normalized
                    or normalized.startswith("server_")
                ):
                    forbidden_keys.append(str(key))
                inspect(item)
            return
        if isinstance(value, list):
            for item in value:
                inspect(item)
            return
        if isinstance(value, str) and (
            PRIVATE_SENTINEL in value
            or "/Users/private" in value
            or "token=" in value.lower()
            or "api_key=" in value.lower()
        ):
            private_values.append(value)

    inspect(projection)
    expected_sources = {
        "openclaw_tcp_probe",
        "openclaw_gateway_protocol",
    }
    return (
        observed_sources == expected_sources
        and not invalid_sources
        and not forbidden_keys
        and not private_values,
        {
            "observed_sources": sorted(observed_sources),
            "invalid_sources": invalid_sources,
            "forbidden_keys": sorted(set(forbidden_keys)),
            "private_values": private_values,
            "projection": projection,
        },
    )


async def run_contract(root: Path) -> None:
    store = WorldStateStore(root / "state")
    adapter = PrivateCapabilityAdapter()
    agency = AgencyCore(
        store,
        agency_root=root / "agency",
        model_assist_enabled=False,
    )
    proactive = ProactiveChecks(
        store,
        agency=agency,
        model_assist_enabled=False,
        agent_adapter_resolver=lambda: adapter,
    )
    proactive.self_heal.probe_runner = lambda port: {
        **probe(
            "openclaw",
            status="listening",
            source="openclaw_probe",
            target="openclaw_runtime",
        ),
        "details": {"port": port},
    }
    proactive._run_probes = lambda *_args, **_kwargs: {
        "system": probe("system"),
        "git": {**probe("git"), "dirty": False},
        "openclaw": probe(
            "openclaw",
            status="closed",
            source="openclaw_probe",
            target="openclaw_runtime",
        ),
        "hermes": probe("hermes"),
        "network": probe("network"),
        "web": probe("web"),
        "mcp": probe("mcp"),
    }

    app = FastAPI()
    app.include_router(
        build_debug_audit_router(
            {
                "state_store": store,
                "agency_core": agency,
                "proactive_checks": proactive,
            }
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://veyra.test",
    ) as client:
        response = await client.post("/proactive/check")
        expect(response.status_code == 200, "proactive check remains available")
        body = response.json()
        expect(
            body.get("self_heal", {}).get("status") == "shadow_healthy"
            and body.get("self_heal", {}).get("attempt_count") == 0,
            "default HTTP path is observation-only shadow",
            body.get("self_heal"),
        )

        before = store.read_json("self_heal_state.json").get("_state_revision")
        status_response = await client.get("/proactive/self-heal/status")
        after = store.read_json("self_heal_state.json").get("_state_revision")
        expect(
            status_response.status_code == 200
            and before == after
            and adapter.connection_calls == 1,
            "status endpoint is read-only and performs no Gateway call",
            {
                "before": before,
                "after": after,
                "connection_calls": adapter.connection_calls,
            },
        )
        post_projection = body.get("self_heal") or {}
        status_projection = status_response.json()
        post_compact, post_detail = compact_verifier_contract(
            post_projection
        )
        status_compact, status_detail = compact_verifier_contract(
            status_projection
        )
        expect(
            post_compact,
            (
                "POST proactive self-heal exposes only compact verifier "
                "evidence"
            ),
            post_detail,
        )
        expect(
            status_compact,
            (
                "dedicated self-heal status exposes only compact verifier "
                "evidence"
            ),
            status_detail,
        )

        state_response = await client.get("/state")
        expect(
            state_response.status_code == 200
            and "self_heal_state" not in state_response.json(),
            "generic state does not expose the private self-heal ledger",
            state_response.json().get("self_heal_state"),
        )
        all_public = json.dumps(
            {
                "proactive": body,
                "status": status_response.json(),
                "state": state_response.json(),
            },
            ensure_ascii=False,
        )
        self_heal_public = json.dumps(
            {
                "proactive": body.get("self_heal"),
                "status": status_response.json(),
            },
            ensure_ascii=False,
        )
        expect(
            PRIVATE_SENTINEL not in all_public
            and "/Users/private" not in all_public
            and "gateway_url" not in self_heal_public
            and "base_url" not in self_heal_public
            and "auth_diagnostics" not in self_heal_public,
            "HTTP projections omit private Gateway identity and errors",
        )


def main() -> None:
    with TemporaryDirectory(prefix="veyra-phase5-http-") as tmp:
        asyncio.run(run_contract(Path(tmp)))
    print("openclaw_reconnect_http_contract_smoke: ok")


if __name__ == "__main__":
    main()
