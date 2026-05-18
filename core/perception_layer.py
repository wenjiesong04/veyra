from __future__ import annotations

from typing import Any

from awareness.belief_core import BeliefCore
from awareness.claim_schema import make_claim
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class PerceptionLayer:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        self.belief = BeliefCore(state_store)

    def interpret_probe_result(self, probe_result: dict[str, Any]) -> dict[str, Any]:
        probe_name = probe_result.get("probe", "unknown")
        observed_at = str(probe_result.get("observed_at") or probe_result.get("timestamp") or utc_now_iso())
        ttl_seconds = int(probe_result.get("ttl_seconds") or 60)
        confidence = float(probe_result.get("confidence") or 0.75)
        enriched = {
            **probe_result,
            "source": probe_result.get("source") or probe_name,
            "observed_at": observed_at,
            "timestamp": probe_result.get("timestamp") or observed_at,
            "ttl_seconds": ttl_seconds,
            "confidence": confidence,
        }
        local_world = self.state_store.read_json("local_world.json")
        probes = local_world.setdefault("probes", {})
        probes[probe_name] = enriched
        local_world["last_probe_at"] = observed_at
        self.state_store.write_json("local_world.json", local_world)

        claims = self._claims_from_probe(enriched)
        self.belief.upsert_claims(claims)
        return {
            "local_world.probes": {probe_name: enriched},
            "belief.claims": claims,
        }

    def _claims_from_probe(self, probe_result: dict[str, Any]) -> list[dict[str, Any]]:
        explicit = probe_result.get("claims")
        if isinstance(explicit, list) and explicit:
            return [self._normalize_claim(probe_result, claim) for claim in explicit if isinstance(claim, dict)]

        probe_name = str(probe_result.get("probe") or "unknown")
        confidence = float(probe_result.get("confidence") or 0.75)
        ttl_seconds = int(probe_result.get("ttl_seconds") or 60)
        observed_at = str(probe_result.get("observed_at") or probe_result.get("timestamp") or utc_now_iso())
        status = str(probe_result.get("status") or "unknown")
        summary = str(probe_result.get("summary") or f"{probe_name} returned {status}")
        evidence = self._compact_evidence(probe_result)

        if probe_name in {"port_probe", "openclaw_probe"}:
            port = probe_result.get("port")
            host = probe_result.get("host", "127.0.0.1")
            runtime = probe_result.get("runtime")
            key = f"{runtime or 'port'}:{host}:{port}:status"
            claim = f"{runtime or 'port'} {host}:{port} is {status}"
        elif probe_name == "git_probe":
            key = "git_workspace:dirty"
            claim = "git workspace has uncommitted changes" if probe_result.get("dirty") else "git workspace is clean"
        elif probe_name == "system_probe":
            key = "local_system:platform"
            claim = summary
        elif probe_name == "process_probe":
            key = "local_process:list_available"
            claim = summary
        elif probe_name == "file_probe":
            key = f"file:{probe_result.get('path')}:exists"
            claim = f"file {probe_result.get('path')} exists={probe_result.get('exists')}"
        elif probe_name == "log_probe":
            key = f"log:{probe_result.get('path')}:status"
            claim = summary
        else:
            key = f"{probe_name}:{probe_result.get('target') or 'default'}:status"
            claim = summary

        return [
            make_claim(
                key=key,
                claim=claim,
                source=probe_name,
                confidence=confidence,
                ttl_seconds=ttl_seconds,
                observed_at=observed_at,
                evidence=evidence,
            )
        ]

    def _normalize_claim(self, probe_result: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
        probe_name = str(probe_result.get("probe") or "unknown")
        return make_claim(
            key=str(claim.get("key") or claim.get("claim") or f"{probe_name}:claim"),
            claim=str(claim.get("claim") or probe_result.get("summary") or ""),
            source=str(claim.get("source") or probe_name),
            confidence=float(claim.get("confidence") or probe_result.get("confidence") or 0.75),
            ttl_seconds=int(claim.get("ttl_seconds") or probe_result.get("ttl_seconds") or 60),
            observed_at=str(claim.get("observed_at") or probe_result.get("observed_at") or probe_result.get("timestamp") or utc_now_iso()),
            evidence=dict(claim.get("evidence") or self._compact_evidence(probe_result)),
        )

    def _compact_evidence(self, probe_result: dict[str, Any]) -> dict[str, Any]:
        skipped = {"claims", "details"}
        return {key: value for key, value in probe_result.items() if key not in skipped}
