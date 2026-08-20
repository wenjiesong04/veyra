#!/usr/bin/env python3
"""Focused production-boundary smoke for the V1 Calendar source.

This is deliberately narrower than the Living Source acceptance smoke.  It
checks that status reads do not invoke a provider, that consent is distinct
from availability, that receipts preserve the selected provider provenance,
and that the macOS adapter keeps its JXA query bounded at the source.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import plistlib
from pathlib import Path
import subprocess
from types import SimpleNamespace
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.living_source_contract import canonical_utc  # noqa: E402
from runtime.calendar_source import CalendarSource, IcsCalendarProvider, MacOSCalendarProvider, _MACOS_JXA  # noqa: E402
from runtime.living_context_composition import build_living_context_composition  # noqa: E402
from runtime.living_source_runtime import LivingSourceRuntime  # noqa: E402
from runtime import living_context_composition as composition_module  # noqa: E402
from scripts.living_source_smoke import FIXED_NOW, NeedCatalog, _binding, _consent  # noqa: E402
from runtime.product_experience import ProductExperienceService  # noqa: E402


def expect(value: bool, label: str) -> None:
    if not value:
        raise AssertionError(label)
    print(f"PASS {label}")


class Provider:
    provider_id = "calendar.test_provider.v1"

    def __init__(self) -> None:
        self.calls = 0

    def read(self, context):
        self.calls += 1
        return {"status": "ok", "events": [], "summary": "bounded test result"}


class RawStatusProvider:
    provider_id = "calendar.raw_status_test.v1"

    def __init__(self, status: str) -> None:
        self.status = status

    def read(self, context):
        return {"status": self.status, "reason": "typed test status", "events": []}


class StatusOrchestrator:
    def __init__(self, *, permission: str, consented: bool, available: bool) -> None:
        self.permission = permission
        self.consented = consented
        self.available = available

    def source_status(self, *, owner_id: str, session_id: str):
        return {
            "status": "ok",
            "capabilities": {
                "calendar": {
                    "enabled": True,
                    "configured": True,
                    "system_permission": self.permission,
                    "available": self.available,
                    "consent_required": True,
                }
            },
            "consent": {"calendar": {"required": True, "granted": self.consented, "generation": 1}},
        }


class CaptureReaction:
    def __init__(self) -> None:
        self.value = None

    def evaluate(self, value):
        self.value = value
        return {"decision": {"reaction_id": "reaction-attemptable", "situation_revision": 1}}


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="veyra-calendar-production-") as temp_dir:
        root = Path(temp_dir)
        catalog = NeedCatalog()
        catalog.add("calendar-production", "calendar")
        provider = Provider()
        runtime = LivingSourceRuntime(
            WorldStateStore(root / "runtime"),
            current_need_resolver=catalog.resolve,
            calendar_source=CalendarSource(provider),
            clock=lambda: FIXED_NOW,
        )
        binding = _binding(
            catalog,
            "calendar-production",
            "calendar",
            {"window_start": canonical_utc(FIXED_NOW + timedelta(days=3)), "window_end": canonical_utc(FIXED_NOW + timedelta(days=4))},
        )
        runtime.register_binding(binding)
        before = runtime.status(user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        calendar_capability = before["capabilities"]["calendar"]
        expect(provider.calls == 0, "status GET does not invoke the provider")
        expect(calendar_capability["configured"] is True, "explicit provider is configured")
        expect(calendar_capability["consented"] is False and calendar_capability["available"] is False and calendar_capability["can_request"] is False, "configured source is unavailable before consent")
        runtime.grant_consent(_consent("calendar"))
        receipt = runtime.request("calendar-production", "calendar", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        expect(receipt.status in {"ok", "empty"} and receipt.payload["provider"] == "calendar.test_provider.v1", "receipt preserves actual provider provenance")
        after = runtime.status(user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        expect(after["capabilities"]["calendar"]["available"] is True, "consent makes a ready source available")

        context = SimpleNamespace(parameters={"window_start": canonical_utc(FIXED_NOW), "window_end": canonical_utc(FIXED_NOW + timedelta(days=1))})
        mac = MacOSCalendarProvider(enabled=True, runner=lambda args, timeout: "[]")
        expect(mac.system_permission == "unknown", "macOS permission starts unconfirmed without a TCC probe")
        expect("whose" in _MACOS_JXA and "cal.events()" not in _MACOS_JXA, "macOS query is bounded before event materialization")
        expect(mac.read(context)["status"] == "empty" and mac.system_permission == "ready", "successful macOS read records ready permission state")
        error_mac = MacOSCalendarProvider(
            enabled=True,
            runner=lambda args, timeout: (_ for _ in ()).throw(
                subprocess.CalledProcessError(
                    1,
                    args,
                    output=b"private stdout detail",
                    stderr=b"execution error: not authorized (-1743)",
                )
            ),
        )
        error_result = error_mac.read(context)
        expect(error_result["status"] == "denied" and error_mac.system_permission == "denied", "TCC denial is classified from bounded stderr/stdout")
        expect("private stdout detail" not in str(error_result), "TCC process output is never echoed")

        unknown_mac = MacOSCalendarProvider(enabled=True, runner=lambda args, timeout: "[]")
        mac_runtime = LivingSourceRuntime(
            WorldStateStore(root / "permission-unknown"),
            current_need_resolver=catalog.resolve,
            calendar_source=CalendarSource(unknown_mac),
            clock=lambda: FIXED_NOW,
        )
        mac_runtime.register_binding(binding)
        unknown_before = mac_runtime.status(user_id="u-v1", session_id="s-v1", now=FIXED_NOW)["capabilities"]["calendar"]
        expect(unknown_before["available"] is False and unknown_before["can_request"] is False, "permission-unknown source cannot request before consent")
        mac_runtime.grant_consent(_consent("calendar"))
        unknown_after_consent = mac_runtime.status(user_id="u-v1", session_id="s-v1", now=FIXED_NOW)["capabilities"]["calendar"]
        expect(unknown_after_consent["available"] is False and unknown_after_consent["can_request"] is True, "consent unlocks one bounded read while permission is unknown")
        first_mac_read = mac_runtime.request("calendar-production", "calendar", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
        expect(first_mac_read.status == "empty" and unknown_mac.system_permission == "ready", "first consented bounded read establishes ready permission")

        capture = CaptureReaction()
        from runtime.living_context_orchestrator import LivingContextOrchestrator
        orchestrator = LivingContextOrchestrator(object(), capture, object(), clock=lambda: FIXED_NOW)
        orchestrator._evaluate_situation(
            {
                "situation_id": "situation-attemptable",
                "owner_id": "u-v1",
                "session_id": "s-v1",
                "revision": 1,
                "title": "Calendar boundary",
                "summary": "A bounded calendar read is needed.",
                "goal": "Keep the situation current.",
                "status": "active",
                "progress": 0.2,
                "risk": "low",
                "known": [],
                "unknown": ["calendar evidence"],
                "evidence": [],
                "material_change": {},
                "deadline_at": None,
            },
            [
                {
                    "need_id": "need-attemptable",
                    "revision": 1,
                    "status": "open",
                    "kind": "missing_context",
                    "question": "What is on the calendar?",
                    "source": "calendar",
                    "priority": 0.8,
                }
            ],
            owner_id="u-v1",
            session_id="s-v1",
            source_status={
                "capabilities": {"calendar": {"available": False, "can_request": True}},
                "consent": {"calendar": {"granted": True}},
            },
        )
        expect(capture.value is not None and capture.value.source_availability.get("calendar") is True, "reaction treats consented permission-unknown source as attemptable read")

        for raw_status in ("denied", "unavailable", "unknown", "timeout"):
            raw_catalog = NeedCatalog()
            need_id = f"raw-{raw_status}"
            raw_catalog.add(need_id, "calendar")
            raw_runtime = LivingSourceRuntime(
                WorldStateStore(root / f"raw-{raw_status}"),
                current_need_resolver=raw_catalog.resolve,
                calendar_source=CalendarSource(RawStatusProvider(raw_status)),
                clock=lambda: FIXED_NOW,
            )
            raw_binding = _binding(raw_catalog, need_id, "calendar", {"window_start": canonical_utc(FIXED_NOW), "window_end": canonical_utc(FIXED_NOW + timedelta(days=1))})
            raw_runtime.register_binding(raw_binding)
            raw_runtime.grant_consent(_consent("calendar"))
            raw_receipt = raw_runtime.request(need_id, "calendar", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
            expect(raw_receipt.status == raw_status and raw_receipt.payload == {} and raw_receipt.ttl_seconds == 0, f"raw {raw_status} status is typed with no payload or TTL")
        denied_mac = MacOSCalendarProvider(enabled=True, system_permission="denied")
        denied_runtime = LivingSourceRuntime(
            WorldStateStore(root / "permission-denied"),
            current_need_resolver=catalog.resolve,
            calendar_source=CalendarSource(denied_mac),
            clock=lambda: FIXED_NOW,
        )
        denied_status = denied_runtime.status(user_id="u-v1", session_id="s-v1", now=FIXED_NOW)["capabilities"]["calendar"]
        expect(denied_status["configured"] is True and denied_status["system_permission"] == "denied" and denied_status["available"] is False, "permission denial remains explicit and unavailable")

        default_composition = build_living_context_composition(WorldStateStore(root / "default-composition"), clock=lambda: FIXED_NOW)
        default_status = default_composition.orchestrator.source_status(owner_id="u-v1", session_id="s-v1")["capabilities"]["calendar"]
        if composition_module.sys.platform == "darwin":
            expect(default_status["configured"] is True and default_status["system_permission"] == "unknown" and default_status["available"] is False, "macOS default Calendar is configured but unavailable before consent and TCC")
        else:
            expect(default_status["configured"] is False and default_status["available"] is False, "non-macOS default Calendar is not configured")
        disabled_composition = build_living_context_composition(
            WorldStateStore(root / "explicit-disabled"),
            calendar_source=CalendarSource(MacOSCalendarProvider(enabled=False)),
            clock=lambda: FIXED_NOW,
        )
        disabled_status = disabled_composition.orchestrator.source_status(owner_id="u-v1", session_id="s-v1")["capabilities"]["calendar"]
        expect(disabled_status["configured"] is False and disabled_status["available"] is False, "explicitly disabled Calendar remains not configured")
        original_platform = composition_module.sys.platform
        try:
            composition_module.sys.platform = "linux"
            non_macos = build_living_context_composition(WorldStateStore(root / "non-macos"), clock=lambda: FIXED_NOW)
            non_macos_status = non_macos.orchestrator.source_status(owner_id="u-v1", session_id="s-v1")["capabilities"]["calendar"]
            expect(non_macos_status["configured"] is False and non_macos_status["available"] is False, "simulated non-macOS default Calendar is not configured")
        finally:
            composition_module.sys.platform = original_platform

        product_unknown = ProductExperienceService(
            WorldStateStore(root / "product-unknown"),
            living_context_orchestrator=StatusOrchestrator(permission="unknown", consented=False, available=False),
        ).sources(user_id="u-v1", session_id="s-v1")
        expect(product_unknown["items"]["calendar"]["status"] == "permission_unknown", "Product keeps permission uncertainty explicit")
        product_consent = ProductExperienceService(
            WorldStateStore(root / "product-consent"),
            living_context_orchestrator=StatusOrchestrator(permission="ready", consented=False, available=False),
        ).sources(user_id="u-v1", session_id="s-v1")
        expect(product_consent["items"]["calendar"]["status"] == "needs_consent", "Product keeps consent separate from availability")

        info = plistlib.loads((ROOT / "apps/desktop/src-tauri/Info.plist").read_bytes())
        entitlements = plistlib.loads((ROOT / "apps/desktop/src-tauri/Entitlements.plist").read_bytes())
        expect("NSAppleEventsUsageDescription" in info, "macOS app declares Apple Events usage")
        expect(entitlements.get("com.apple.security.automation.apple-events") is True, "macOS app declares automation entitlement")
    return {"status": "passed", "checks": ["status_purity", "consent_availability", "provider_provenance", "bounded_jxa", "macos_default_wiring", "permission_denied", "product_projection", "macos_bundle_metadata"]}


if __name__ == "__main__":
    print("LIVING_CALENDAR_PRODUCTION_SMOKE_OK", run())
