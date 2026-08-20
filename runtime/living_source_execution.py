"""Provider execution leases and finalization fence helpers."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from typing import Any

from common.living_source_primitives import MAX_FLIGHT_LEASES, SourceAdmissionError


def lease_active(runtime: Any, lease_key: str) -> bool:
    with runtime._lease_lock:
        lease = runtime._leases.get(lease_key)
        if lease is None:
            return False
        future = lease["future"]
        if not future.done():
            return True
        if runtime._leases.get(lease_key) is lease:
            runtime._leases.pop(lease_key, None)
        lease["executor"].shutdown(wait=False, cancel_futures=True)
        return False


def execution_status(runtime: Any) -> dict[str, Any]:
    """Return the bounded in-process provider lease projection.

    A timed-out provider may still be running because Python threads cannot be
    safely killed.  Such a lease remains counted until its future completes;
    this is deliberately visible to callers instead of being reported as an
    available execution slot.
    """

    with runtime._lease_lock:
        leases = list(runtime._leases.values())
        lease_count = len(leases)
        running_count = sum(1 for lease in leases if not lease["future"].done())
        timed_out_count = sum(1 for lease in leases if bool(lease.get("timed_out")))
    at_capacity = lease_count >= MAX_FLIGHT_LEASES
    degraded = bool(timed_out_count or at_capacity)
    return {
        "status": "degraded" if degraded else "ok",
        "lease_count": lease_count,
        "running_count": running_count,
        "timed_out_lease_count": timed_out_count,
        "capacity": MAX_FLIGHT_LEASES,
        "available_slots": max(0, MAX_FLIGHT_LEASES - lease_count),
        "reason": (
            "timed_out_provider_leases_retained"
            if timed_out_count
            else "provider_lease_capacity_exhausted"
            if at_capacity
            else ""
        ),
    }


def invoke(runtime: Any, provider: Any, context: Any, *, timeout: float, lease_key: str) -> tuple[Any, str]:
    if provider is None:
        return None, "unavailable"
    with runtime._lease_lock:
        if lease_active(runtime, lease_key):
            return None, "inflight"
        if len(runtime._leases) >= MAX_FLIGHT_LEASES:
            return None, "unavailable"
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="veyra-source")
        try:
            future: Future[Any] = executor.submit(provider.read, context)
        except Exception:
            executor.shutdown(wait=False, cancel_futures=True)
            return None, "unavailable"
        lease: dict[str, Any] = {"future": future, "executor": executor}
        runtime._leases[lease_key] = lease

        def release(done: Future[Any]) -> None:
            with runtime._lease_lock:
                if runtime._leases.get(lease_key) is lease:
                    runtime._leases.pop(lease_key, None)
            executor.shutdown(wait=False, cancel_futures=True)

        future.add_done_callback(release)
    try:
        value = future.result(timeout=max(0.05, timeout))
        return value, "completed"
    except FutureTimeout:
        # ``Future.cancel`` cannot stop a provider that has already started.
        # Retain the lease as a long-lived single-flight reservation until the
        # done callback observes completion; the MAX_FLIGHT_LEASES cap then
        # fails new providers closed instead of allowing overlap.
        with runtime._lease_lock:
            if runtime._leases.get(lease_key) is lease:
                lease["timed_out"] = True
        return None, "timeout"
    except Exception:
        return None, "unavailable"


def finalization_fence(runtime: Any, binding: Any, now: datetime) -> tuple[str | None, str]:
    try:
        runtime._assert_current_binding(binding)
    except SourceAdmissionError as exc:
        reason = str(exc).lower()
        status = "expired" if "not active" in reason or "expired" in reason else "stale"
        return status, f"current_need_fence:{type(exc).__name__}"
    if not binding.active_at(now):
        return "expired", "source_binding_expired_before_finalize"
    return None, ""


__all__ = ["execution_status", "finalization_fence", "invoke", "lease_active"]
