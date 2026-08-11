from __future__ import annotations

from pathlib import Path
from typing import Any

from awareness.belief_economy import economy_value
from core.perception_layer import PerceptionLayer
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from awareness.refresh_spec import (
    REFRESH_RESOLVER_DEFAULT,
    REFRESH_RESOLVER_LITERAL,
    validate_refresh_spec,
)
from interface.event_schema import utc_now_iso
from probes.git_probe import GitProbe
from probes.hermes_probe import HermesProbe
from probes.mcp_probe import McpProbe
from probes.network_probe import NetworkProbe
from probes.openclaw_probe import OpenClawProbe
from probes.port_probe import PortProbe
from probes.process_probe import ProcessProbe
from probes.system_probe import SystemProbe
from probes.time_probe import TimeProbe
from probes.web_probe import WebProbe


class StateRefresh:
    """Refreshes stale belief claims through known read-only probes."""

    def __init__(self, state_store: WorldStateStore, reasoning: CoreReasoning | None = None, *, model_assist_enabled: bool = True) -> None:
        self.state_store = state_store
        self.reasoning = reasoning or CoreReasoning(state_store)
        self.perception = PerceptionLayer(state_store, reasoning=self.reasoning, model_assist_enabled=model_assist_enabled)
        #: Probes whose observation is meaningless without an explicit target.
        #: Running them with an empty argument produces a `missing_target`
        #: result that describes the call, not the world.
        self.TARGET_REQUIRED_PROBES = frozenset(
            {"web_probe", "network_probe", "port_probe"}
        )
        self.probes = {
            "git_probe": GitProbe(),
            "hermes_probe": HermesProbe(),
            "mcp_probe": McpProbe(),
            "network_probe": NetworkProbe(),
            "openclaw_probe": OpenClawProbe(),
            "port_probe": PortProbe(),
            "process_probe": ProcessProbe(),
            "system_probe": SystemProbe(),
            "time_probe": TimeProbe(),
            "web_probe": WebProbe(),
        }

    def refresh_stale(self, limit: int = 20) -> dict[str, Any]:
        claims = self.state_store.read_json("belief_state.json").get("claims", [])
        stale = [
            claim
            for claim in claims
            if isinstance(claim, dict) and claim.get("status") in {"stale", "expired", "conflict"} and claim.get("next_action") == "refresh_probe"
        ]
        limit = max(1, int(limit))
        supported = [claim for claim in stale if str(claim.get("source") or "") in self.probes]
        unsupported = [claim for claim in stale if str(claim.get("source") or "") not in self.probes]
        selected, cursor_before, cursor_after = self._select_fair_batch(supported, limit=limit)
        refreshed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        unresolvable: list[dict[str, Any]] = []
        for claim in selected:
            source = str(claim.get("source") or "")
            probe = self.probes.get(source)
            if probe is None:
                continue
            target = self._target_for_claim(claim)
            if target is None:
                # Fail closed: a probe that needs a target must not run without
                # one. Guessing a target from the claim's own prose made failed
                # observations reproduce themselves once per tick.
                unresolvable.append(
                    {
                        "claim": claim.get("key") or claim.get("claim"),
                        "source": source,
                        "reason": "no_resolvable_refresh_target",
                    }
                )
                continue
            raw = probe.run(target)
            raw = {
                **raw,
                "refresh_mode": "stale_claim",
                **{
                    key: claim.get(key)
                    for key in (
                        "scope_kind",
                        "tenant_derived",
                        "user_id",
                        "session_id",
                    )
                    if claim.get(key) is not None
                },
            }
            patch = self.perception.interpret_probe_result(raw)
            persistence = patch.get("belief_persistence") if isinstance(patch, dict) else None
            persistence_status = (
                str(persistence.get("status") or "")
                if isinstance(persistence, dict)
                else ""
            )
            entry = {
                "claim": claim.get("key") or claim.get("claim"),
                "probe_result": raw,
                "state_patch": patch,
            }
            if persistence is None:
                # Keep compatibility with bounded test doubles and older
                # perception adapters, which returned an explicit accepted
                # status without the detailed persistence envelope.
                if patch.get("status") in {None, "accepted", "success"}:
                    refreshed.append(entry)
                else:
                    failed.append({**entry, "reason": f"belief_persistence_{patch.get('status')}"})
            elif persistence_status in {"accepted", "accepted_with_conflict"}:
                refreshed.append(entry)
            else:
                # A probe can be successful while its observation is
                # conflicted or rejected.  It must not be counted as a
                # successful refresh until the Belief value is accepted.
                failed.append(
                    {
                        **entry,
                        "reason": f"belief_persistence_{persistence_status or 'unknown'}",
                    }
                )
        skipped = [
            {
                "claim": claim.get("key") or claim.get("claim"),
                "source": claim.get("source"),
                "reason": f"no probe for source {claim.get('source')}",
            }
            for claim in unsupported[:limit]
        ] + unresolvable
        def record_refresh_batch(state: dict[str, Any]) -> None:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "supported_count": len(supported),
                    "unsupported_count": len(unsupported),
                    "last_selected": [
                        str(claim.get("key") or claim.get("claim") or "")
                        for claim in selected
                    ],
                }
            )

        self.state_store.mutate_json("state_refresh_state.json", record_refresh_batch)
        return {
            "status": "degraded" if failed else "success",
            "refreshed": refreshed,
            "failed": failed,
            "skipped": skipped,
            "remaining_stale": max(0, len(supported) - len(refreshed)),
            "unsupported_stale": len(unsupported),
            "cursor": {"before": cursor_before, "after": cursor_after},
        }

    def _select_fair_batch(self, claims: list[dict[str, Any]], *, limit: int) -> tuple[list[dict[str, Any]], int, int]:
        if not claims:
            self.state_store.patch_json(
                "state_refresh_state.json",
                {"cursor": 0, "updated_at": utc_now_iso()},
            )
            return [], 0, 0
        ordered = sorted(
            claims,
            key=self._refresh_sort_key,
        )
        count = min(limit, len(ordered))
        selected: list[dict[str, Any]] = []
        cursor_before = 0
        cursor_after = 0

        def reserve_batch(state: dict[str, Any]) -> None:
            nonlocal cursor_before, cursor_after
            try:
                cursor_before = int(state.get("cursor") or 0) % len(ordered)
            except (TypeError, ValueError):
                cursor_before = 0
            selected.extend(
                ordered[(cursor_before + offset) % len(ordered)]
                for offset in range(count)
            )
            cursor_after = (cursor_before + count) % len(ordered)
            state["cursor"] = cursor_after
            state["updated_at"] = utc_now_iso()

        self.state_store.mutate_json("state_refresh_state.json", reserve_batch)
        return selected, cursor_before, cursor_after

    @staticmethod
    def _status_priority(claim: dict[str, Any]) -> int:
        # Hard stale/expired/conflict obligations are ordered before a merely
        # old claim.  The caller already filters to refreshable claims, but a
        # deterministic rank keeps malformed status values fail-closed.
        return {
            "expired": 0,
            "conflict": 1,
            "stale": 2,
        }.get(str(claim.get("status") or ""), 3)

    @classmethod
    def _refresh_sort_key(cls, claim: dict[str, Any]) -> tuple[Any, ...]:
        value = economy_value(claim.get("economy"))
        return (
            cls._status_priority(claim),
            0 if value is not None else 1,
            -float(value or 0.0),
            str(claim.get("updated_at") or claim.get("observed_at") or ""),
            cls._owner_key(claim),
            str(claim.get("key") or claim.get("claim") or ""),
        )

    @staticmethod
    def _owner_key(claim: dict[str, Any]) -> str:
        return ":".join(
            [
                str(claim.get("user_id") or "ownerless"),
                str(claim.get("session_id") or "sessionless"),
            ]
        )

    def _target_for_claim(self, claim: dict[str, Any]) -> str | None:
        """Resolve a structured refresh target, or None when none is available.

        Returning None means "do not probe". A claim's human-readable text is
        not a probe argument: passing it back made a failed observation
        reproduce itself, because the summary "Web probe needs an http or https
        URL." was handed to WebProbe as the URL.

        This is the narrow form of the refresh hygiene rule; the full
        ``refresh_spec`` (probe_kind + target_ref + resolver_id) is a later
        slice. Until then, probes that need a target fail closed, and probes
        that need none keep receiving an empty string.
        """

        source = str(claim.get("source") or "")
        if "refresh_spec" in claim:
            try:
                spec = validate_refresh_spec(claim.get("refresh_spec"), source=source)
            except ValueError:
                return None
            if spec["resolver_id"] == REFRESH_RESOLVER_LITERAL:
                return spec["target_ref"]
            if spec["resolver_id"] == REFRESH_RESOLVER_DEFAULT:
                return ""
            return None

        evidence = claim.get("evidence") if isinstance(claim.get("evidence"), dict) else {}
        for key in ("target", "url", "host", "path"):
            if evidence.get(key):
                return str(evidence[key])
        details = evidence.get("details") if isinstance(evidence.get("details"), dict) else {}
        for key in ("target", "url", "host", "path", "port"):
            if details.get(key):
                value = str(details[key])
                if key == "path" and not Path(value).exists():
                    return None
                return value
        if source in self.TARGET_REQUIRED_PROBES:
            return None
        return ""
