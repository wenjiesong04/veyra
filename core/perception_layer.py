from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from awareness.belief_core import BeliefCore
from awareness.claim_schema import make_claim
from core.context_scope import (
    OPERATOR_GLOBAL_SCOPE,
    TENANT_SCOPE,
    probe_scope_metadata,
    tenant_scope_storage_key,
)
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


#: A probe that never reached a target did not observe the world. Its summary
#: is an operator hint about the call ("needs an http or https URL"), not a
#: claim about reality, so it must not become a Belief. Persisting it also fed
#: a refresh loop: the stale claim was re-selected, its own text was used as the
#: next probe target, and the failure reproduced itself on every tick.
_NON_OBSERVATION_STATUSES = frozenset({"missing_target"})


class PerceptionLayer:
    def __init__(self, state_store: WorldStateStore, reasoning: CoreReasoning | None = None, *, model_assist_enabled: bool = True) -> None:
        self.state_store = state_store
        self.belief = BeliefCore(state_store)
        self.reasoning = reasoning or CoreReasoning(state_store)
        self.model_assist_enabled = model_assist_enabled

    def interpret_probe_result(self, probe_result: dict[str, Any]) -> dict[str, Any]:
        probe_name = probe_result.get("probe", "unknown")
        observed_at = str(probe_result.get("observed_at") or probe_result.get("timestamp") or utc_now_iso())
        ttl_seconds = int(probe_result.get("ttl_seconds") or 60)
        confidence = float(probe_result.get("confidence") or 0.75)
        scope_metadata = probe_scope_metadata(
            probe_result,
            probe_name=str(probe_name),
        )
        enriched = {
            **probe_result,
            **scope_metadata,
            "source": probe_result.get("source") or probe_name,
            "observed_at": observed_at,
            "timestamp": probe_result.get("timestamp") or observed_at,
            "ttl_seconds": ttl_seconds,
            "expires_at": self._expires_at(observed_at, ttl_seconds),
            "confidence": confidence,
            "fact_kind": "observation",
        }
        anomaly = self._detect_anomaly(enriched)
        if anomaly:
            enriched["anomaly"] = anomaly
            details = dict(enriched.get("details") or {})
            details["anomaly"] = anomaly
            enriched["details"] = details
        model_assist = self.reasoning.perception_assist(enriched) if self._should_model_interpret(enriched) else {"status": "skipped", "reason": "deterministic_perception"}
        if model_assist.get("status") == "model_assisted":
            enriched["model_interpretation"] = self._compact_model_interpretation(model_assist)
            model_anomaly = model_assist.get("anomaly") if isinstance(model_assist.get("anomaly"), dict) else {}
            if model_anomaly and not enriched.get("anomaly"):
                enriched["anomaly"] = {
                    "kind": str(model_anomaly.get("kind") or "model_detected"),
                    "next_action": str(model_anomaly.get("next_action") or "review"),
                }
        observation = dict(enriched)
        model_interpretation = observation.pop("model_interpretation", None)

        def update_local_world(local_world: dict[str, Any]) -> dict[str, Any]:
            if scope_metadata.get("scope_kind") == OPERATOR_GLOBAL_SCOPE:
                probes = local_world.setdefault("probes", {})
                if not isinstance(probes, dict):
                    probes = {}
                    local_world["probes"] = probes
                probes[probe_name] = observation
            elif (
                scope_metadata.get("scope_kind") == TENANT_SCOPE
                and scope_metadata.get("user_id")
                and scope_metadata.get("session_id")
                and not scope_metadata.get("scope_status")
            ):
                scoped_probes = local_world.setdefault(
                    "scoped_probes",
                    {},
                )
                if not isinstance(scoped_probes, dict):
                    scoped_probes = {}
                    local_world["scoped_probes"] = scoped_probes
                scope_key = tenant_scope_storage_key(
                    scope_metadata["user_id"],
                    scope_metadata["session_id"],
                )
                scope_bucket = (
                    scoped_probes.get(scope_key)
                    if isinstance(scoped_probes.get(scope_key), dict)
                    else {}
                )
                scope_bucket[probe_name] = observation
                scoped_probes[scope_key] = scope_bucket
            local_world["last_probe_at"] = observed_at
            return local_world

        self.state_store.mutate_json("local_world.json", update_local_world)

        claims = self._claims_from_probe(enriched) + self._claims_from_model(enriched, model_assist)
        for claim in claims:
            claim.update(scope_metadata)
        self.belief.upsert_claims(claims)
        local_path = (
            "local_world.probes"
            if scope_metadata.get("scope_kind") == OPERATOR_GLOBAL_SCOPE
            else "local_world.scoped_probes"
            if scope_metadata.get("user_id")
            and scope_metadata.get("session_id")
            and not scope_metadata.get("scope_status")
            else "local_world.not_persisted"
        )
        result = {
            local_path: {probe_name: enriched},
            "belief.claims": claims,
        }
        if model_interpretation:
            result["model_interpretation"] = model_interpretation
        return result

    def _claims_from_probe(self, probe_result: dict[str, Any]) -> list[dict[str, Any]]:
        explicit = probe_result.get("claims")
        if isinstance(explicit, list) and explicit:
            return [self._normalize_claim(probe_result, claim) for claim in explicit if isinstance(claim, dict)]

        if str(probe_result.get("status") or "") in _NON_OBSERVATION_STATUSES:
            return []

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
                claim_kind="observed",
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
            claim_kind="observed",
        )

    def _compact_evidence(self, probe_result: dict[str, Any]) -> dict[str, Any]:
        skipped = {"claims", "details"}
        return {key: value for key, value in probe_result.items() if key not in skipped}

    def _detect_anomaly(self, probe_result: dict[str, Any]) -> dict[str, str] | None:
        text = " ".join(
            [
                str(probe_result.get("status", "")),
                str(probe_result.get("summary", "")),
                str(probe_result.get("details", "")),
            ]
        ).lower()
        patterns = {
            "connection_refused": ["connection refused", "errno 61", "connect call failed"],
            "timeout": ["timed out", "timeout"],
            "auth_required": ["auth", "unauthorized", "forbidden", "token", "password"],
            "protocol_mismatch": ["protocol mismatch", "unsupported protocol", "incompatible_gateway"],
        }
        for kind, markers in patterns.items():
            if any(marker in text for marker in markers):
                return {"kind": kind, "next_action": "refresh_probe" if kind in {"timeout", "connection_refused"} else "request_configuration"}
        return None

    def _should_model_interpret(self, probe_result: dict[str, Any]) -> bool:
        if not self.model_assist_enabled:
            return False
        details = probe_result.get("details") if isinstance(probe_result.get("details"), dict) else {}
        if probe_result.get("model_assist") is False or details.get("model_assist") is False or details.get("perception_model_assist") is False:
            return False
        probe_name = str(probe_result.get("probe") or "")
        if probe_name in {
            "time_probe",
            "system_probe",
            "port_probe",
            "git_probe",
            "process_probe",
            "network_probe",
            "openclaw_probe",
            "hermes_probe",
            "mcp_probe",
        } and not probe_result.get("anomaly"):
            return False
        return probe_name in {"web_probe", "log_probe", "file_probe", "openclaw_probe", "hermes_probe", "mcp_probe"} or bool(probe_result.get("anomaly"))

    def _claims_from_model(self, probe_result: dict[str, Any], model_assist: dict[str, Any]) -> list[dict[str, Any]]:
        if model_assist.get("status") != "model_assisted":
            return []
        raw_claims = model_assist.get("claims")
        if not isinstance(raw_claims, list):
            return []
        claims: list[dict[str, Any]] = []
        probe_name = str(probe_result.get("probe") or "unknown")
        observed_at = str(probe_result.get("observed_at") or probe_result.get("timestamp") or utc_now_iso())
        probe_confidence = float(probe_result.get("confidence") or 0.75)
        for index, raw_claim in enumerate(raw_claims[:5]):
            if not isinstance(raw_claim, dict):
                continue
            claim_text = str(raw_claim.get("claim") or "").strip()
            if not claim_text:
                continue
            confidence = min(float(raw_claim.get("confidence") or 0.65), probe_confidence, 0.8)
            claims.append(
                make_claim(
                    key=str(raw_claim.get("key") or f"model:{probe_name}:{index}"),
                    claim=claim_text,
                    source=f"core_model:{probe_name}",
                    confidence=confidence,
                    ttl_seconds=int(raw_claim.get("ttl_seconds") or probe_result.get("ttl_seconds") or 60),
                    observed_at=observed_at,
                    evidence={"probe": probe_name, "model_assisted": True},
                    claim_kind="derived",
                    derived_from=f"probe:{probe_name}:{observed_at}",
                )
            )
        return claims

    def _compact_model_interpretation(self, model_assist: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": str(model_assist.get("summary") or "")[:800],
            "anomaly": model_assist.get("anomaly") if isinstance(model_assist.get("anomaly"), dict) else None,
            "confidence": model_assist.get("confidence"),
        }

    def _expires_at(self, observed_at: str, ttl_seconds: int) -> str:
        try:
            parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed + timedelta(seconds=max(0, ttl_seconds))).isoformat()
