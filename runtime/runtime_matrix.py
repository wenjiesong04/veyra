from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from interface.agent_registry import AgentRegistry
from interface.provider_certification import certify_agent_provider
from memory_bridge.local_memory_bridge import LocalMemoryBridge


class RuntimeMatrix:
    """Runs a read-mostly capability matrix across configured Agent runtimes."""

    def __init__(
        self,
        state_store: WorldStateStore,
        registry: AgentRegistry,
        memory_bridge: LocalMemoryBridge,
        *,
        ttl_seconds: int = 1800,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.registry = registry
        self.memory_bridge = memory_bridge
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def status(self) -> dict[str, Any]:
        current = self.state_store.read_json("ops_runtime_matrix.json") or {"status": "not_run", "runtimes": []}
        current = dict(current)
        current.setdefault(
            "validation",
            {
                "codebase": "implemented",
                "configured": 0,
                "validated": 0,
                "status": "not_run",
            },
        )
        self._apply_freshness(current)
        return current

    def run(self, *, write_memory_probe: bool = False) -> dict[str, Any]:
        self.registry.refresh()
        runtimes = [self._runtime_row(name, write_memory_probe=write_memory_probe) for name in self.registry.names()]
        summary = {
            "total": len(runtimes),
            "ready": len([item for item in runtimes if item["status"] == "ready"]),
            "degraded": len([item for item in runtimes if item["status"] == "degraded"]),
            "not_configured": len([item for item in runtimes if item["status"] == "not_configured"]),
            "error": len([item for item in runtimes if item["status"] == "error"]),
        }
        status = "ready" if summary["ready"] and not (summary["degraded"] or summary["error"]) else "not_configured" if summary["not_configured"] == summary["total"] else "degraded"
        observed = self._now()
        observed_at = self._iso(observed)
        ttl_seconds = self.ttl_seconds
        result = {
            "status": status,
            "checked_at": observed_at,
            "observed_at": observed_at,
            "expires_at": self._iso(observed + timedelta(seconds=ttl_seconds)),
            "ttl_seconds": ttl_seconds,
            "freshness": "fresh",
            "age_seconds": 0,
            "write_memory_probe": write_memory_probe,
            "summary": summary,
            "validation": {
                "codebase": "implemented",
                "configured": summary["total"] - summary["not_configured"],
                "validated": summary["ready"],
                "status": "validated" if summary["ready"] else "validation_pending" if summary["degraded"] or summary["error"] else "not_configured",
            },
            "runtimes": runtimes,
        }
        self.state_store.write_json("ops_runtime_matrix.json", result)
        self.state_store.append_jsonl("action_record.jsonl", {"route": "ops_runtime_matrix", "status": status, "artifacts": redact_sensitive(result, max_string=2200)})
        return result

    def _apply_freshness(self, current: dict[str, Any]) -> None:
        persisted_status = str(current.get("status") or "not_run")
        if persisted_status == "not_run":
            current.setdefault("freshness", "not_observed")
            current.setdefault("age_seconds", None)
            current.setdefault("observed_at", None)
            current.setdefault("expires_at", None)
            return

        ttl_seconds = self._ttl(current.get("ttl_seconds"))
        observed_at = str(current.get("observed_at") or current.get("checked_at") or current.get("updated_at") or "")
        observed = self._parse_timestamp(observed_at)
        current["ttl_seconds"] = ttl_seconds
        current["observed_at"] = observed_at or None
        current["expires_at"] = self._iso(observed + timedelta(seconds=ttl_seconds)) if observed else None

        if observed is None:
            current["freshness"] = "unknown"
            current["age_seconds"] = None
            self._mark_stale(current, persisted_status, reason="missing_or_invalid_observed_at")
            return

        age_seconds = max(0, int((self._now() - observed).total_seconds()))
        current["age_seconds"] = age_seconds
        if age_seconds <= ttl_seconds:
            current["freshness"] = "fresh"
            return

        current["freshness"] = "stale"
        self._mark_stale(current, persisted_status, reason="runtime_matrix_ttl_expired")

    def _mark_stale(self, current: dict[str, Any], persisted_status: str, *, reason: str) -> None:
        current["observed_status"] = str(current.get("observed_status") or persisted_status)
        current["status"] = "stale"
        validation = dict(current.get("validation") or {})
        validation["observed_status"] = str(validation.get("observed_status") or validation.get("status") or "unknown")
        validation["status"] = "validation_pending"
        validation["validated"] = 0
        validation["reason"] = reason
        current["validation"] = validation

    def _ttl(self, value: Any) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return self.ttl_seconds

    def _now(self) -> datetime:
        value = self._now_fn()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _parse_timestamp(self, value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _iso(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    def _runtime_row(self, name: str, *, write_memory_probe: bool) -> dict[str, Any]:
        adapter = self.registry.get(name)
        try:
            connection = adapter.connection_status()
        except Exception as exc:
            return {"name": name, "status": "error", "error": str(exc)}
        capabilities = (
            connection.get("capabilities")
            if isinstance(connection.get("capabilities"), dict)
            else connection
        )
        observed_at = self._now()
        trusted_native_adapter = bool(
            getattr(
                adapter,
                "trusted_native_provider_adapter",
                False,
            )
        )
        try:
            provider_certification = certify_agent_provider(
                runtime=name,
                capabilities=capabilities,
                observed_at=observed_at,
                now=observed_at,
                ttl_seconds=self.ttl_seconds,
                trusted_native_adapter=trusted_native_adapter,
            )
        except (TypeError, ValueError) as exc:
            provider_certification = {
                "certification_status": "unverified",
                "validated": False,
                "issues": [
                    f"provider_certification_failed:{type(exc).__name__}"
                ],
                "read_only_dispatch_allowed": False,
                "automatic_selection_allowed": False,
                "provider_switch_allowed": False,
                "side_effect_dispatch_allowed": False,
                "policy_effect": "none",
            }
        adapter_binding = {
            "trusted_native_adapter": trusted_native_adapter,
            "source": "local_adapter_instance",
        }
        if provider_certification.get("validated") is not True:
            row_status = self._row_status(
                connection,
                capabilities,
                {"status": "blocked"},
                {"status": "blocked"},
                provider_certification,
            )
            return {
                "name": name,
                "status": row_status,
                "validation": {
                    "implemented": True,
                    "configured": row_status != "not_configured",
                    "validated": False,
                    "status": (
                        "not_configured"
                        if row_status == "not_configured"
                        else "validation_pending"
                    ),
                },
                "adapter_binding": adapter_binding,
                "connection": redact_sensitive(
                    connection,
                    max_string=1000,
                ),
                "capabilities": redact_sensitive(
                    capabilities,
                    max_string=1000,
                ),
                "provider_certification": redact_sensitive(
                    provider_certification,
                    max_string=1000,
                ),
                "memory": {
                    "status": "blocked",
                    "reason": "provider_certification_required",
                    "write_probe": {
                        "status": "skipped",
                        "reason": "provider_certification_required",
                    },
                },
                "task_status": {
                    "status": "blocked",
                    "reason": "provider_certification_required",
                },
            }
        try:
            memory = self.memory_bridge.provider_diagnostics(provider=name, session_id=f"runtime-matrix-{name}", write_probe=write_memory_probe)
        except Exception as exc:
            memory = {"status": "error", "error": str(exc)}
        try:
            task_status = adapter.fetch_task_status("veyra_runtime_matrix_probe").to_dict()
        except Exception as exc:
            task_status = {"status": "error", "error": str(exc)}
        row_status = self._row_status(
            connection,
            capabilities,
            memory,
            task_status,
            provider_certification,
        )
        return {
            "name": name,
            "status": row_status,
            "validation": {
                "implemented": True,
                "configured": row_status != "not_configured",
                "validated": row_status == "ready",
                "status": "validated" if row_status == "ready" else "validation_pending" if row_status in {"degraded", "error"} else "not_configured",
            },
            "connection": redact_sensitive(connection, max_string=1000),
            "capabilities": redact_sensitive(capabilities, max_string=1000),
            "adapter_binding": adapter_binding,
            "provider_certification": redact_sensitive(
                provider_certification,
                max_string=1000,
            ),
            "memory": redact_sensitive(memory, max_string=1000),
            "task_status": redact_sensitive(task_status, max_string=1000),
        }

    def _row_status(
        self,
        connection: dict[str, Any],
        capabilities: dict[str, Any],
        memory: dict[str, Any],
        task_status: dict[str, Any],
        provider_certification: dict[str, Any],
    ) -> str:
        statuses = {str(connection.get("status")), str(capabilities.get("status")), str(memory.get("status")), str(task_status.get("status"))}
        if "adapter_unconfigured" in statuses or "not_configured" in statuses:
            return "not_configured"
        if "error" in statuses or "unavailable" in statuses:
            return "error"
        connection_validation = connection.get("validation") if isinstance(connection.get("validation"), dict) else {}
        connection_ready = connection_validation.get("validated") is True
        capabilities_ready = str(capabilities.get("status")) in {"available", "ok", "success"}
        task_poll_ready = str(task_status.get("status")) in {"success", "submitted", "running", "pending"}
        memory_acceptable = str(memory.get("status")) in {"success", "degraded"} and str(memory.get("summary", {}).get("status") if isinstance(memory.get("summary"), dict) else "") != "error"
        provider_ready = provider_certification.get("validated") is True
        if connection_ready and capabilities_ready and task_poll_ready and memory_acceptable and provider_ready:
            return "ready"
        return "degraded"
