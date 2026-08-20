"""Governed, read-only source registry and receipt controller for Veyra V1."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
from typing import Any, Callable, Mapping, Protocol

from core.world_state import WorldStateStore
from common.living_source_primitives import (
    MAX_ATTEMPTS,
    MAX_BINDINGS,
    MAX_CONSENTS,
    MAX_FLIGHT_LEASES,
    MAX_RECEIPTS,
    MAX_RETRY_DELAY_SECONDS,
    RETRY_BASE_SECONDS,
    RUNTIME_SCHEMA,
    SourceAdmissionError,
    SourceCapacityError,
    SourceStateCorruptError,
    LivingSourceRuntimeError,
    _ACTIVE_NEED_STATUSES,
    _RETRYABLE_RECEIPTS,
    _SOURCE_POLICY_CLASS,
    _TERMINAL_REQUESTS,
    _aware_now,
    parse_time,
)
from interface.living_source_contract import (
    SourceCapability,
    SourceConsent,
    SourceContext,
    SourceKind,
    SourceNeedBinding,
    SourceReceipt,
    SourceRequest,
    canonical_utc,
    capability_registry,
    consent_digest_from_record,
    make_receipt_id,
    make_request_id,
    stable_digest,
    utc_now,
)
from probes.search_probe import SearchProbe
from probes.weather_probe import WeatherProbe
from runtime.calendar_source import CalendarSource, DisabledCalendarProvider
from runtime.living_source_projection import project_provider_result
from runtime.living_source_execution import execution_status, finalization_fence, invoke, lease_active
from runtime.living_source_state import (
    binding_from_dict,
    consent_from_dict,
    receipt_from_dict,
    request_from_dict,
    scope_status,
    validate_state,
)


class SourceProvider(Protocol):
    def read(self, context: SourceContext) -> dict[str, Any]:
        ...


NeedResolver = Callable[[str, str, str], Mapping[str, Any] | None]


class _UserAnswerProvider:
    provider_id = "user_answer.pending.v1"

    def read(self, context: SourceContext) -> dict[str, Any]:
        raise SourceAdmissionError("user_answer is completed by the upper conversation layer")


class _UnavailableAgentResearchProvider:
    provider_id = "agent_research.unavailable.v1"

    def read(self, context: SourceContext) -> dict[str, Any]:
        return {"status": "unavailable", "reason": "agent_research_not_configured"}


class _WeatherProvider:
    provider_id = "weather.open_meteo.v1"

    def __init__(self, probe: WeatherProbe | None = None) -> None:
        self.probe = probe or WeatherProbe()

    def read(self, context: SourceContext) -> dict[str, Any]:
        return self.probe.run(location=str(context.parameters.get("location") or "").strip())


class _PublicWebProvider:
    provider_id = "public_web.search_probe.v1"

    def __init__(self, probe: SearchProbe | None = None) -> None:
        self.probe = probe or SearchProbe()

    def read(self, context: SourceContext) -> dict[str, Any]:
        return self.probe.run(str(context.parameters.get("query") or "").strip(), max_results=int(context.parameters.get("max_results", 5)))


class LivingSourceRuntime:
    """Server-owned source lifecycle backed by one WorldStateStore document."""

    def __init__(self, state_store: WorldStateStore, *, current_need_resolver: NeedResolver | None = None, state_file: str = "living_source_state.json", providers: Mapping[str, SourceProvider] | None = None, weather_probe: WeatherProbe | None = None, search_probe: SearchProbe | None = None, calendar_source: CalendarSource | None = None, clock: Callable[[], datetime] | None = None, max_receipts: int = MAX_RECEIPTS) -> None:
        if not isinstance(state_store, WorldStateStore):
            raise TypeError("LivingSourceRuntime requires a WorldStateStore")
        if not state_file or state_file.startswith(("/", "\\")) or ".." in state_file.split("/"):
            raise ValueError("state_file must be a server-owned relative file")
        self.state_store = state_store
        self.state_file = state_file
        self.current_need_resolver = current_need_resolver
        self._clock = clock or utc_now
        self._lock = threading.RLock()
        self._lease_lock = threading.RLock()
        self._leases: dict[str, dict[str, Any]] = {}
        self._max_receipts = max(1, min(int(max_receipts), MAX_RECEIPTS))
        self._capabilities: dict[str, SourceCapability] = capability_registry()
        self._providers: dict[str, SourceProvider] = {"user_answer": _UserAnswerProvider(), "calendar": calendar_source or CalendarSource(DisabledCalendarProvider()), "weather": _WeatherProvider(weather_probe), "public_web": _PublicWebProvider(search_probe), "agent_research": _UnavailableAgentResearchProvider()}
        if providers:
            for source, provider in providers.items():
                if source not in self._capabilities:
                    raise SourceAdmissionError(f"source {source!r} is not registered")
                self._providers[source] = provider
        # The Living Context command seam uses the already constructed source
        # controller to validate receipt identity/currentness.  This is an
        # in-memory reference only; the durable source state remains the
        # authority and is revalidated on every read.
        setattr(state_store, "_living_source_runtime", self)

    def _empty_state(self) -> dict[str, Any]:
        return {"schema_version": RUNTIME_SCHEMA, "bindings": {}, "consents": {}, "requests": {}, "receipts": {}, "request_keys": {}}

    def _validated_state(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        return validate_state(self, raw)

    def _read_state(self) -> dict[str, Any]:
        return self._validated_state(self.state_store.read_json(self.state_file))

    def _mutate_state(self, mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        def guarded(current: dict[str, Any]) -> dict[str, Any]:
            return mutator(self._validated_state(current))
        return self.state_store.mutate_json(self.state_file, guarded)

    def capabilities(self) -> dict[str, dict[str, Any]]:
        return {name: capability.to_dict() for name, capability in self._capabilities.items()}

    def status(self, *, user_id: str, session_id: str, now: datetime | None = None) -> dict[str, Any]:
        """Return a bounded, pure-read projection for one exact scope."""

        result = scope_status(self, user_id=user_id, session_id=session_id, now=now)
        lease_projection = execution_status(self)
        result["execution"] = lease_projection
        if result.get("status") == "ok" and lease_projection["status"] == "degraded":
            result["status"] = "degraded"
            result["reason"] = lease_projection["reason"]
        return result

    def _current_need(self, binding: SourceNeedBinding) -> dict[str, Any] | None:
        if self.current_need_resolver is None:
            if binding.source == "user_answer":
                return None
            raise SourceAdmissionError("current InformationNeed resolver is required for real source reads")
        try:
            raw = self.current_need_resolver(binding.user_id, binding.session_id, binding.need_id)
        except Exception as exc:
            raise SourceAdmissionError("current InformationNeed resolver failed") from exc
        if not isinstance(raw, Mapping):
            raise SourceAdmissionError("current InformationNeed is unavailable")
        owner = str(raw.get("owner_id", raw.get("user_id", "")) or "")
        session = str(raw.get("session_id", "") or "")
        need_id = str(raw.get("need_id", "") or "")
        if owner != binding.user_id or session != binding.session_id or need_id != binding.need_id:
            raise SourceAdmissionError("current InformationNeed scope or identity mismatch")
        revision_raw = raw.get("need_revision", raw.get("generation"))
        if isinstance(revision_raw, bool) or not isinstance(revision_raw, int) or revision_raw < 1:
            raise SourceAdmissionError("current InformationNeed revision is unavailable")
        digest = str(raw.get("record_digest") or raw.get("need_digest") or "")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise SourceAdmissionError("current InformationNeed digest is invalid")
        allowed = raw.get("allowed_source_classes", raw.get("source_classes"))
        if not isinstance(allowed, (list, tuple, set)):
            raise SourceAdmissionError("current InformationNeed source policy is unavailable")
        return {"owner_id": owner, "session_id": session, "need_id": need_id, "situation_id": str(raw.get("situation_id") or ""), "revision": revision_raw, "record_digest": digest, "status": str(raw.get("status") or ""), "allowed": {str(item) for item in allowed}}

    def _assert_current_binding(self, binding: SourceNeedBinding) -> dict[str, Any] | None:
        current = self._current_need(binding)
        if current is None:
            return None
        if current["situation_id"] != binding.situation_id or current["revision"] != binding.need_revision or current["record_digest"] != binding.record_digest:
            raise SourceAdmissionError("source binding is stale for the current InformationNeed revision")
        if current["status"] not in _ACTIVE_NEED_STATUSES:
            raise SourceAdmissionError("InformationNeed is not active")
        policy_class = _SOURCE_POLICY_CLASS[binding.source]
        if policy_class not in current["allowed"] and binding.source not in current["allowed"]:
            raise SourceAdmissionError("source is not allowed by the current InformationNeed policy")
        return current

    def register_binding(self, binding: SourceNeedBinding) -> SourceNeedBinding:
        if not isinstance(binding, SourceNeedBinding):
            raise SourceAdmissionError("register_binding requires a typed core need binding")
        self._assert_current_binding(binding)
        result: dict[str, SourceNeedBinding] = {}
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            existing = state["bindings"].get(binding.binding_id)
            if existing is not None:
                current = self._binding_from_dict(existing)
                if current.to_dict() != binding.to_dict():
                    raise SourceAdmissionError("binding id is already bound to another core need revision")
                result["binding"] = current
                return state
            if len(state["bindings"]) >= MAX_BINDINGS:
                self._reclaim_bindings(state, now=_aware_now(self._clock))
            if len(state["bindings"]) >= MAX_BINDINGS:
                raise SourceCapacityError(
                    "source binding capacity is exhausted; referenced/current bindings are retained"
                )
            state["bindings"][binding.binding_id] = binding.to_dict()
            result["binding"] = binding
            return state
        self._mutate_state(mutate)
        return result["binding"]

    def _reclaim_bindings(self, state: dict[str, Any], *, now: datetime) -> int:
        """Drop only unreferenced expired/non-current binding generations.

        Bindings are the server-issued bridge from an InformationNeed to a
        source.  A request or receipt retains the bridge as audit lineage even
        after it becomes terminal, so reclamation must not remove any binding
        referenced by *any* retained request.  Active/current bindings are
        likewise protected; if no safe row can be reclaimed, the caller keeps
        the capacity error.
        """

        referenced = {
            str(raw.get("binding_id") or "")
            for raw in state.get("requests", {}).values()
            if isinstance(raw, Mapping) and raw.get("binding_id")
        }
        removable: list[tuple[str, SourceNeedBinding]] = []
        for binding_id, raw in state.get("bindings", {}).items():
            if binding_id in referenced or not isinstance(raw, Mapping):
                continue
            binding = self._binding_from_dict(raw)
            # Currentness is authoritative even after the binding's own TTL.
            # An expired binding may still be the exact bridge for the current
            # Need generation, and deleting it would erase the server's only
            # current lineage.  Resolver failures/absence are unprovable, so
            # they are retained (fail closed) rather than treated as stale.
            try:
                current = self._current_need(binding)
            except Exception:
                continue
            if current is None:
                continue
            is_current = not (
                current.get("situation_id") != binding.situation_id
                or int(current.get("revision") or 0) != binding.need_revision
                or str(current.get("record_digest") or "") != binding.record_digest
                or str(current.get("status") or "") not in _ACTIVE_NEED_STATUSES
                or (_SOURCE_POLICY_CLASS.get(binding.source) not in current.get("allowed", set()) and binding.source not in current.get("allowed", set()))
            )
            if is_current:
                continue
            # Only an already expired binding with resolver-proven stale
            # currentness is reclaimable.  A live stale row is allowed to
            # age out, keeping this maintenance path fail-closed.
            if not binding.active_at(now):
                removable.append((binding_id, binding))
        # Deterministic oldest-first collection makes capacity behavior
        # reproducible without making the binding id an eviction authority.
        removable.sort(key=lambda item: (item[1].expires_at, item[1].issued_at, item[0]))
        for binding_id, _ in removable:
            state["bindings"].pop(binding_id, None)
        return len(removable)

    def grant_consent(self, consent: SourceConsent) -> SourceConsent:
        if not isinstance(consent, SourceConsent):
            raise SourceAdmissionError("grant_consent requires a typed consent")
        result: dict[str, SourceConsent] = {}
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            existing_raw = state["consents"].get(consent.consent_id)
            selected = consent
            if existing_raw is not None:
                existing = self._consent_from_dict(existing_raw)
                if existing.scope != consent.scope or existing.source != consent.source:
                    raise SourceAdmissionError("consent id cannot be rebound across owner/session/source")
                if existing.granted and existing.active_at(_aware_now(self._clock)):
                    if existing.to_dict() != consent.to_dict():
                        # A caller that has observed the current generation may
                        # renew it in place.  The generation is the CAS/audit
                        # fence; the old grant is never silently rebound.
                        if int(consent.generation) not in {existing.generation, existing.generation + 1}:
                            raise SourceAdmissionError("consent generation is stale")
                        selected = replace(consent, generation=existing.generation + 1)
                    else:
                        selected = existing
                else:
                    # Expired grants remain an audit row.  Renewal/regrant is
                    # a new generation, including when the caller supplied a
                    # stale constructor default of generation=1.
                    selected = replace(consent, generation=existing.generation + 1)
            elif len(state["consents"]) >= MAX_CONSENTS:
                raise SourceCapacityError("consent capacity is exhausted")
            state["consents"][selected.consent_id] = selected.to_dict()
            result["consent"] = selected
            return state
        self._mutate_state(mutate)
        return result["consent"]

    def revoke_consent(self, consent_id: str, *, user_id: str, session_id: str) -> bool:
        changed = {"value": False}
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            raw = state["consents"].get(str(consent_id))
            if not isinstance(raw, dict):
                return state
            consent = self._consent_from_dict(raw)
            self._assert_scope(consent.scope, user_id=user_id, session_id=session_id)
            if consent.granted:
                state["consents"][consent.consent_id] = replace(consent, granted=False).to_dict()
                changed["value"] = True
            self._revoke_scope_receipts(state, user_id=consent.user_id, session_id=consent.session_id, source=consent.source, consent_id=consent.consent_id)
            return state
        self._mutate_state(mutate)
        return bool(changed["value"])

    def _resolve_binding_for_admission(self, state: Mapping[str, Any], *, need_id: str, source: str, user_id: str, session_id: str, binding_id: str | None) -> SourceNeedBinding:
        candidates: list[SourceNeedBinding] = []
        for raw in state["bindings"].values():
            if not isinstance(raw, dict):
                continue
            binding = self._binding_from_dict(raw)
            if binding.need_id == str(need_id) and binding.source == source and binding.scope == (str(user_id), str(session_id)) and (binding_id is None or binding.binding_id == binding_id):
                candidates.append(binding)
        if not candidates:
            raise SourceAdmissionError("unknown server-issued source binding")
        candidates.sort(key=lambda item: (item.need_revision, item.issued_at, item.binding_id), reverse=True)
        last_error: Exception | None = None
        for binding in candidates:
            try:
                self._assert_current_binding(binding)
                return binding
            except SourceAdmissionError as exc:
                last_error = exc
        raise SourceAdmissionError("no current source binding matches the InformationNeed") from last_error

    def admit(self, need_id: str, source: SourceKind, *, user_id: str, session_id: str, now: datetime | None = None, binding_id: str | None = None) -> SourceRequest:
        selected_now = (now or _aware_now(self._clock)).astimezone(timezone.utc)
        self._assert_scope((str(user_id), str(session_id)), user_id=user_id, session_id=session_id)
        result: dict[str, SourceRequest] = {}
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            binding = self._resolve_binding_for_admission(state, need_id=str(need_id), source=source, user_id=user_id, session_id=session_id, binding_id=binding_id)
            capability = self._capabilities[source]
            key = f"{binding.binding_id}:{source}"
            existing_id = state["request_keys"].get(key)
            existing: SourceRequest | None = None
            existing_receipt: SourceReceipt | None = None
            if existing_id:
                existing = self._request_from_dict(state["requests"].get(existing_id, {}))
                if existing.receipt_id:
                    existing_receipt = self._receipt_from_dict(state["receipts"].get(existing.receipt_id, {}))
                    self._assert_receipt_matches_request(existing_receipt, existing)
            consent = self._active_consent(state, binding, selected_now) if capability.consent_required else None
            if capability.consent_required and consent is None and existing_receipt is not None and existing_receipt.status in {"ok", "empty", "pending"}:
                self._revoke_request_receipt(state, existing, reason="consent_revoked")
                existing = self._request_from_dict(state["requests"][existing.request_id])
                existing_receipt = self._receipt_from_dict(state["receipts"][existing.receipt_id]) if existing.receipt_id else None
            if existing is not None and existing_receipt is not None:
                if existing_receipt.status == "pending":
                    result["request"] = existing
                    return state
                if existing_receipt.status in {"ok", "empty"} and existing_receipt.is_fresh(selected_now):
                    result["request"] = existing
                    return state
                if existing_receipt.status in _RETRYABLE_RECEIPTS and self._retry_due(existing, existing_receipt, selected_now):
                    if self._lease_active(key):
                        result["request"] = existing
                        return state
                    pass
                elif existing_receipt.status in {"revoked", "denied", "expired", "stale"} and consent is not None:
                    pass
                elif existing_receipt.status in {"ok", "empty"} and not existing_receipt.is_fresh(selected_now):
                    pass
                else:
                    result["request"] = existing
                    return state
            rollover = bool(existing is not None and existing_receipt is not None and existing_receipt.status in _RETRYABLE_RECEIPTS and existing.attempt >= MAX_ATTEMPTS)
            # Keep an attempt epoch in the request identity.  Using the
            # binding issue time for every retry would collide with an older
            # epoch's attempt=2 row and leave its receipt reverse reference
            # invalid during state validation.
            request_seed = existing.issued_at if existing is not None else binding.issued_at
            attempt = (existing.attempt + 1) if existing is not None else 1
            if rollover:
                # Keep the exhausted request/receipt as terminal audit until
                # bounded retention needs the space, and start a new bounded
                # attempt epoch.  This is what lets a transient outage recover
                # after MAX_ATTEMPTS without an unbounded attempt counter.
                attempt = 1
                request_seed = canonical_utc(selected_now)
            self._ensure_capacity_for_request(state, now=selected_now)
            status = "admitted"
            reason = ""
            if not capability.enabled:
                status, reason = "denied", "source_capability_disabled"
            elif not binding.active_at(selected_now):
                status, reason = "expired", "source_binding_expired"
            elif capability.consent_required and consent is None:
                status, reason = "denied", "explicit_consent_required"
            parameters = binding.parameters_for(source)
            request = SourceRequest(request_id=make_request_id(binding.binding_id, source, f"{request_seed}:{attempt}"), need_id=binding.need_id, user_id=binding.user_id, workspace_id=binding.workspace_id, session_id=binding.session_id, source=source, issued_at=canonical_utc(selected_now), expires_at=binding.expires_at, timeout_seconds=min(float(capability.max_timeout_seconds), 10.0), parameters_digest=stable_digest(parameters, namespace="source.parameters"), binding_id=binding.binding_id, attempt=attempt, consent_id=consent.consent_id if consent else None, consent_generation=consent.generation if consent else 0, consent_digest=consent_digest_from_record(consent.to_dict()) if consent else None, status=status)
            state["requests"][request.request_id] = request.to_dict()
            state["request_keys"][key] = request.request_id
            if status != "admitted":
                receipt = self._receipt_in_state(state, request, status="expired" if status == "expired" else "denied", reason=reason, now=selected_now, payload={}, ttl_seconds=0, generation=0)
                request = replace(request, status=status, receipt_id=receipt.receipt_id)
                state["requests"][request.request_id] = request.to_dict()
            result["request"] = request
            return state
        self._mutate_state(mutate)
        return result["request"]

    def request(self, need_id: str, source: SourceKind, *, user_id: str, session_id: str, now: datetime | None = None, binding_id: str | None = None) -> SourceReceipt:
        admitted = self.admit(need_id, source, user_id=user_id, session_id=session_id, now=now, binding_id=binding_id)
        return self.execute(admitted.request_id, user_id=user_id, session_id=session_id, now=now)

    def execute(self, request_id: str, *, user_id: str, session_id: str, now: datetime | None = None) -> SourceReceipt:
        selected_now = (now or _aware_now(self._clock)).astimezone(timezone.utc)
        claim: dict[str, Any] = {}
        def claim_mutation(state: dict[str, Any]) -> dict[str, Any]:
            request = self._request_from_dict(state["requests"].get(str(request_id), {}))
            self._assert_scope(request.scope, user_id=user_id, session_id=session_id)
            binding = self._binding_from_state(state, request.need_id, request.source, binding_id=request.binding_id)
            self._assert_current_binding(binding)
            capability = self._capabilities[request.source]
            consent = self._active_consent(state, binding, selected_now) if capability.consent_required else None
            if capability.consent_required and consent is None:
                if request.status == "denied" and request.receipt_id:
                    claim["receipt"] = self._receipt_from_dict(state["receipts"].get(request.receipt_id, {}))
                    return state
                revoked = self._revoke_request_receipt(state, request, reason="consent_revoked") if request.receipt_id else self._receipt_in_state(state, request, status="revoked", reason="consent_revoked", now=selected_now, payload={}, ttl_seconds=0, generation=request.generation)
                state["requests"][request.request_id] = replace(request, status="revoked", receipt_id=revoked.receipt_id).to_dict()
                claim["receipt"] = revoked
                return state
            if capability.consent_required and request.status in {"admitted", "running", "pending"} and not self._consent_matches(consent, request):
                raise SourceStateCorruptError("active source request consent binding is invalid")
            if request.receipt_id:
                receipt = self._receipt_from_dict(state["receipts"].get(request.receipt_id, {}))
                self._assert_receipt_matches_request(receipt, request)
                if receipt.status == "pending":
                    if selected_now - parse_time(receipt.observed_at) > timedelta(seconds=max(1.0, request.timeout_seconds * 2)):
                        recovered = self._receipt_in_state(state, request, status="unknown", reason="inflight_recovery_timeout", now=selected_now, payload={}, ttl_seconds=0, generation=request.generation, receipt_id=receipt.receipt_id, replace_existing=True)
                        state["requests"][request.request_id] = replace(request, status="unknown", receipt_id=recovered.receipt_id).to_dict()
                        claim["receipt"] = recovered
                    else:
                        claim["receipt"] = receipt
                    return state
                claim["receipt"] = receipt
                return state
            if request.status != "admitted":
                unknown = self._receipt_in_state(state, request, status="unknown", reason="request_not_admitted", now=selected_now, payload={}, ttl_seconds=0, generation=request.generation)
                state["requests"][request.request_id] = replace(request, status="unknown", receipt_id=unknown.receipt_id).to_dict()
                claim["receipt"] = unknown
                return state
            if parse_time(request.expires_at) <= selected_now:
                expired = self._receipt_in_state(state, request, status="expired", reason="source_request_expired", now=selected_now, payload={}, ttl_seconds=0, generation=request.generation)
                state["requests"][request.request_id] = replace(request, status="expired", receipt_id=expired.receipt_id).to_dict()
                claim["receipt"] = expired
                return state
            parameters = binding.parameters_for(request.source)
            if stable_digest(parameters, namespace="source.parameters") != request.parameters_digest:
                unknown = self._receipt_in_state(state, request, status="unknown", reason="request_parameter_digest_mismatch", now=selected_now, payload={}, ttl_seconds=0, generation=request.generation)
                state["requests"][request.request_id] = replace(request, status="unknown", receipt_id=unknown.receipt_id).to_dict()
                claim["receipt"] = unknown
                return state
            self._ensure_capacity_for_request(state, adding_request=False)
            generation = request.generation + 1
            pending = self._receipt_in_state(state, request, status="pending", reason="source_read_inflight", now=selected_now, payload={}, ttl_seconds=0, generation=generation)
            claimed = replace(request, status="pending" if request.source == "user_answer" else "running", receipt_id=pending.receipt_id, generation=generation)
            state["requests"][request.request_id] = claimed.to_dict()
            claim.update({"request": claimed, "binding": binding, "parameters": parameters, "provider": self._providers.get(request.source), "capability": capability, "receipt": pending})
            return state
        self._mutate_state(claim_mutation)
        if "request" not in claim or claim["request"].source == "user_answer":
            return claim["receipt"]
        request = claim["request"]
        context = SourceContext(binding=claim["binding"], request=request, parameters=claim["parameters"], now=selected_now)
        raw, invocation_status = self._invoke(claim["provider"], context, timeout=min(request.timeout_seconds, claim["capability"].max_timeout_seconds), lease_key=f"{request.binding_id}:{request.source}")
        if invocation_status == "inflight":
            return claim["receipt"]
        try:
            provider_id = str(getattr(claim.get("provider"), "provider_id", "") or claim["capability"].provider_id)
            return self._finalize(request, claim["receipt"].receipt_id, raw, invocation_status, selected_now, claim["capability"], provider_id=provider_id)
        except Exception as exc:
            # The fallback keeps a provider failure from leaving a request
            # inflight; the finalizer itself remains fail-closed.
            return self._mark_unknown(request, claim["receipt"].receipt_id, f"source_finalization_error:{type(exc).__name__}")

    def submit_user_answer(self, request_id: str, answer: str, *, user_id: str, session_id: str, now: datetime | None = None) -> SourceReceipt:
        selected_now = (now or _aware_now(self._clock)).astimezone(timezone.utc)
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 4000:
            raise SourceAdmissionError("user answer must be non-empty and bounded")
        result: dict[str, SourceReceipt] = {}
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            request = self._request_from_dict(state["requests"].get(str(request_id), {}))
            self._assert_scope(request.scope, user_id=user_id, session_id=session_id)
            binding = self._binding_from_state(state, request.need_id, request.source, binding_id=request.binding_id)
            self._assert_current_binding(binding)
            if request.source != "user_answer" or not request.receipt_id:
                raise SourceAdmissionError("request is not awaiting a user answer")
            current = self._receipt_from_dict(state["receipts"].get(request.receipt_id, {}))
            self._assert_receipt_matches_request(current, request)
            if current.status == "ok":
                result["receipt"] = current
                return state
            if current.status != "pending":
                raise SourceAdmissionError("user-answer request is no longer pending")
            payload = {"facts": {"answer": answer.strip()}, "provider": "user_answer.input.v1", "source": "user_answer"}
            receipt = self._receipt_in_state(state, request, status="ok", reason="user_answer_received", now=selected_now, payload=payload, ttl_seconds=86400, generation=request.generation, receipt_id=current.receipt_id, replace_existing=True)
            state["requests"][request.request_id] = replace(request, status="completed", receipt_id=receipt.receipt_id).to_dict()
            result["receipt"] = receipt
            return state
        self._mutate_state(mutate)
        return result["receipt"]

    def get_binding(self, need_id: str, source: SourceKind, *, user_id: str, session_id: str, binding_id: str | None = None) -> SourceNeedBinding | None:
        state = self._read_state()
        try:
            return self._resolve_binding_for_admission(state, need_id=str(need_id), source=source, user_id=user_id, session_id=session_id, binding_id=binding_id)
        except SourceAdmissionError:
            return None

    def get_request(self, request_id: str, *, user_id: str, session_id: str) -> SourceRequest | None:
        state = self._read_state()
        raw = state["requests"].get(str(request_id))
        if not isinstance(raw, dict):
            return None
        request = self._request_from_dict(raw)
        self._assert_scope(request.scope, user_id=user_id, session_id=session_id)
        binding = self._binding_from_state(state, request.need_id, request.source, binding_id=request.binding_id)
        self._assert_current_binding(binding)
        return request

    def get_receipt(self, receipt_id: str, *, user_id: str, session_id: str, now: datetime | None = None) -> SourceReceipt | None:
        state = self._read_state()
        raw = state["receipts"].get(str(receipt_id))
        if not isinstance(raw, dict):
            return None
        selected_now = now or _aware_now(self._clock)
        receipt = self._receipt_from_dict(raw)
        self._assert_scope(receipt.scope, user_id=user_id, session_id=session_id)
        request = self._request_from_dict(state["requests"].get(receipt.request_id, {}))
        self._assert_receipt_matches_request(receipt, request)
        binding = self._binding_from_state(state, receipt.need_id, receipt.source, binding_id=receipt.binding_id)
        self._assert_current_binding(binding)
        active_consent = self._active_consent(state, binding, selected_now)
        if self._capabilities[receipt.source].consent_required and (
            active_consent is None or not self._consent_matches(active_consent, receipt)
        ):
            return self._redacted_revoked_receipt(receipt)
        if receipt.status in {"ok", "empty"} and not receipt.is_fresh(selected_now):
            return self._redacted_stale_receipt(receipt)
        return receipt

    def list_receipts(self, *, user_id: str, session_id: str, now: datetime | None = None) -> list[SourceReceipt]:
        selected_now = now or _aware_now(self._clock)
        state = self._read_state()
        result: list[SourceReceipt] = []
        for raw in state["receipts"].values():
            if not isinstance(raw, dict):
                continue
            receipt = self._receipt_from_dict(raw)
            if receipt.scope != (str(user_id), str(session_id)):
                continue
            try:
                binding = self._binding_from_state(state, receipt.need_id, receipt.source, binding_id=receipt.binding_id)
                self._assert_current_binding(binding)
            except SourceAdmissionError:
                continue
            active_consent = self._active_consent(state, binding, selected_now)
            if self._capabilities[receipt.source].consent_required and (
                active_consent is None or not self._consent_matches(active_consent, receipt)
            ):
                receipt = self._redacted_revoked_receipt(receipt)
            elif receipt.status in {"ok", "empty"} and not receipt.is_fresh(selected_now):
                receipt = self._redacted_stale_receipt(receipt)
            result.append(receipt)
        return result

    def state_snapshot(self) -> dict[str, Any]:
        try:
            state = self._read_state()
        except SourceStateCorruptError as exc:
            return {"schema_version": RUNTIME_SCHEMA, "status": "degraded", "state_corrupt": True, "reason": str(exc), "capabilities": self.capabilities()}
        receipts: dict[str, Any] = {}
        for key, raw in state["receipts"].items():
            receipt = self._receipt_from_dict(raw)
            receipts[key] = self._redacted_revoked_receipt(receipt).to_dict() if receipt.status == "revoked" else receipt.to_dict()
        return {"schema_version": RUNTIME_SCHEMA, "status": "ok", "state_corrupt": False, "revision": state.get("_state_revision", 0), "capabilities": self.capabilities(), "bindings": dict(state["bindings"]), "consents": dict(state["consents"]), "requests": dict(state["requests"]), "receipts": receipts}

    def _lease_active(self, lease_key: str) -> bool:
        return lease_active(self, lease_key)

    def _invoke(self, provider: SourceProvider | None, context: SourceContext, *, timeout: float, lease_key: str) -> tuple[Any, str]:
        return invoke(self, provider, context, timeout=timeout, lease_key=lease_key)

    def _finalize(
        self,
        request: SourceRequest,
        receipt_id: str,
        raw: Any,
        invocation_status: str,
        started_at: datetime,
        capability: SourceCapability,
        *,
        provider_id: str | None = None,
    ) -> SourceReceipt:
        result: dict[str, SourceReceipt] = {}
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            current_request = self._request_from_dict(state["requests"].get(request.request_id, {}))
            current_receipt = self._receipt_from_dict(state["receipts"].get(receipt_id, {}))
            self._assert_receipt_matches_request(current_receipt, current_request)
            if current_request.generation != request.generation or current_request.receipt_id != receipt_id or current_receipt.generation != request.generation or current_receipt.status != "pending":
                result["receipt"] = current_receipt
                return state
            completion_now = _aware_now(self._clock)
            if completion_now < started_at:
                completion_now = started_at
            binding = self._binding_from_state(state, current_request.need_id, current_request.source, binding_id=current_request.binding_id)
            fence_status, fence_reason = self._finalization_fence(binding, completion_now)
            if fence_status is not None:
                final = self._receipt_in_state(state, current_request, status=fence_status, reason=fence_reason, now=completion_now, payload={}, ttl_seconds=0, generation=request.generation, receipt_id=receipt_id, replace_existing=True)
                state["requests"][request.request_id] = replace(current_request, status=fence_status, receipt_id=receipt_id).to_dict()
                result["receipt"] = final
                return state
            selected_provider_id = str(provider_id or getattr(self._providers.get(request.source), "provider_id", "") or capability.provider_id)
            status, payload, reason, ttl_seconds = project_provider_result(request.source, raw, invocation_status=invocation_status, provider_id=selected_provider_id, default_ttl_seconds=capability.default_ttl_seconds)
            raw_status = str(raw.get("status") or "").strip().lower() if isinstance(raw, Mapping) else ""
            # Provider status is a typed boundary, not evidence.  Preserve a
            # known non-success status (including denied) and strip all facts
            # and freshness from it before durable receipt construction.
            if invocation_status == "timeout":
                status, reason = "timeout", "source_provider_timeout"
            elif invocation_status == "unavailable":
                status, reason = "unavailable", "source_provider_unavailable"
            elif raw_status in {"denied", "unavailable", "unknown", "timeout"}:
                status = raw_status
                reason = {
                    "denied": "source_provider_denied",
                    "unavailable": "source_provider_unavailable",
                    "unknown": "source_provider_unknown",
                    "timeout": "source_provider_timeout",
                }[raw_status]
            if status not in {"ok", "empty"}:
                payload, ttl_seconds = {}, 0
            final = self._receipt_in_state(state, current_request, status=status, reason=reason, now=completion_now, payload=payload, ttl_seconds=ttl_seconds, generation=request.generation, receipt_id=receipt_id, replace_existing=True)
            state["requests"][request.request_id] = replace(current_request, status="completed" if status in {"ok", "empty"} else status, receipt_id=receipt_id).to_dict()
            result["receipt"] = final
            return state
        self._mutate_state(mutate)
        return result["receipt"]

    def _finalization_fence(self, binding: SourceNeedBinding, now: datetime) -> tuple[str | None, str]:
        return finalization_fence(self, binding, now)

    def _mark_unknown(self, request: SourceRequest, receipt_id: str, reason: str) -> SourceReceipt:
        result: dict[str, SourceReceipt] = {}
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            current_request = self._request_from_dict(state["requests"].get(request.request_id, {}))
            current_receipt = self._receipt_from_dict(state["receipts"].get(receipt_id, {}))
            self._assert_receipt_matches_request(current_receipt, current_request)
            if current_receipt.status != "pending":
                result["receipt"] = current_receipt
                return state
            unknown = self._receipt_in_state(state, current_request, status="unknown", reason=reason, now=max(_aware_now(self._clock), parse_time(current_receipt.observed_at)), payload={}, ttl_seconds=0, generation=current_request.generation, receipt_id=receipt_id, replace_existing=True)
            state["requests"][current_request.request_id] = replace(current_request, status="unknown", receipt_id=receipt_id).to_dict()
            result["receipt"] = unknown
            return state
        self._mutate_state(mutate)
        return result["receipt"]

    def _receipt_in_state(self, state: dict[str, Any], request: SourceRequest, *, status: str, reason: str, now: datetime, payload: Mapping[str, Any], ttl_seconds: int, generation: int, receipt_id: str | None = None, replace_existing: bool = False) -> SourceReceipt:
        selected_id = receipt_id or make_receipt_id(request.request_id, f"generation:{generation}")
        if selected_id in state["receipts"] and not replace_existing:
            return self._receipt_from_dict(state["receipts"][selected_id])
        selected_status = status if status in {"ok", "empty", "pending", "unknown", "timeout", "unavailable", "denied", "expired", "stale", "revoked"} else "unknown"
        selected_payload = dict(payload)
        if selected_status not in {"ok", "empty"}:
            selected_payload = {}
            ttl_seconds = 0
        fresh_until = canonical_utc(min(now + timedelta(seconds=ttl_seconds), parse_time(request.expires_at))) if ttl_seconds > 0 and selected_status in {"ok", "empty"} else None
        receipt = SourceReceipt(receipt_id=selected_id, request_id=request.request_id, need_id=request.need_id, user_id=request.user_id, workspace_id=request.workspace_id, session_id=request.session_id, source=request.source, status=selected_status, observed_at=canonical_utc(now), fresh_until=fresh_until, ttl_seconds=max(0, min(int(ttl_seconds), 604800)), payload=selected_payload, reason=str(reason or "")[:600], generation=generation, binding_id=request.binding_id, consent_id=request.consent_id, consent_generation=request.consent_generation, consent_digest=request.consent_digest)
        state["receipts"][receipt.receipt_id] = receipt.to_dict()
        return receipt

    def _ensure_capacity_for_request(self, state: dict[str, Any], *, adding_request: bool = True, now: datetime | None = None) -> None:
        selected_now = (now or _aware_now(self._clock)).astimezone(timezone.utc)
        if not ((adding_request and len(state["requests"]) >= self._max_receipts) or len(state["receipts"]) >= self._max_receipts):
            return
        self._compact_terminal_indexes(state, now=selected_now)
        while (adding_request and len(state["requests"]) >= self._max_receipts) or len(state["receipts"]) >= self._max_receipts:
            candidate_id = next((rid for rid, raw in self._oldest_terminal_requests(state) if rid not in state["request_keys"].values()), None)
            if candidate_id is None:
                raise SourceCapacityError("living source capacity is exhausted; referenced/inflight records are retained")
            raw = state["requests"].pop(candidate_id, None)
            if isinstance(raw, dict) and raw.get("receipt_id"):
                state["receipts"].pop(raw["receipt_id"], None)

    def _compact_terminal_indexes(self, state: dict[str, Any], *, now: datetime) -> None:
        """Drop request-key references that no longer represent live lineage.

        A terminal request is still kept as an audit row when capacity allows,
        but keeping it in ``request_keys`` forever makes the bounded store
        impossible to reclaim.  Fresh successful receipts and inflight rows
        remain current-lineage protected; stale/failed/revoked rows may be
        reclaimed and will be re-admitted by the next call.
        """

        protected: set[str] = set()
        for request_id, raw in state["requests"].items():
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or "")
            if status in {"admitted", "running", "pending"}:
                protected.add(request_id)
                continue
            receipt_id = raw.get("receipt_id")
            receipt = state["receipts"].get(receipt_id) if receipt_id else None
            if isinstance(receipt, dict) and receipt.get("status") in {"ok", "empty"}:
                try:
                    if parse_time(str(receipt.get("fresh_until"))) > now:
                        protected.add(request_id)
                except Exception:
                    pass
        for key, request_id in list(state["request_keys"].items()):
            if request_id not in protected:
                raw = state["requests"].get(request_id)
                if isinstance(raw, dict) and raw.get("status") in _TERMINAL_REQUESTS:
                    state["request_keys"].pop(key, None)

    def _oldest_terminal_requests(self, state: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        rows: list[tuple[str, dict[str, Any]]] = []
        for request_id, raw in state["requests"].items():
            if not isinstance(raw, dict) or raw.get("status") not in _TERMINAL_REQUESTS:
                continue
            rows.append((request_id, raw))
        rows.sort(key=lambda pair: str(pair[1].get("issued_at") or ""))
        return rows

    @staticmethod
    def _retry_due(request: SourceRequest, receipt: SourceReceipt, now: datetime) -> bool:
        """Bound retry pressure while allowing eventual recovery.

        The delay is derived from the terminal receipt, so it survives a
        restart without another mutable scheduler row.  Once the fourth
        attempt has elapsed, the next call starts a fresh attempt epoch.
        """

        try:
            observed = parse_time(receipt.observed_at)
        except LivingSourceRuntimeError:
            return False
        exponent = max(0, min(int(request.attempt) - 1, MAX_ATTEMPTS))
        delay = min(MAX_RETRY_DELAY_SECONDS, RETRY_BASE_SECONDS * (2**exponent))
        return now >= observed + timedelta(seconds=delay)

    def _binding_from_state(self, state: Mapping[str, Any], need_id: str, source: str, *, binding_id: str | None = None) -> SourceNeedBinding:
        raw = state["bindings"].get(binding_id) if binding_id else None
        if not isinstance(raw, dict):
            raise SourceAdmissionError("unknown server-issued source binding")
        binding = self._binding_from_dict(raw)
        if binding.need_id != str(need_id) or binding.source != source:
            raise SourceStateCorruptError("stored binding does not match request identity")
        return binding

    def _active_consent(self, state: Mapping[str, Any], binding: SourceNeedBinding, now: datetime) -> SourceConsent | None:
        matches: list[SourceConsent] = []
        for raw in state["consents"].values():
            consent = self._consent_from_dict(raw)
            if consent.scope == binding.scope and consent.source == binding.source and consent.active_at(now):
                matches.append(consent)
        return max(matches, key=lambda item: item.generation, default=None)

    def _revoke_scope_receipts(self, state: dict[str, Any], *, user_id: str, session_id: str, source: str, consent_id: str) -> None:
        for raw in list(state["requests"].values()):
            if not isinstance(raw, dict):
                continue
            request = self._request_from_dict(raw)
            if request.scope == (str(user_id), str(session_id)) and request.source == source and request.consent_id == consent_id and request.receipt_id:
                self._revoke_request_receipt(state, request, reason="consent_revoked")

    def _revoke_request_receipt(self, state: dict[str, Any], request: SourceRequest, *, reason: str) -> SourceReceipt:
        if not request.receipt_id:
            receipt = self._receipt_in_state(state, request, status="revoked", reason=reason, now=_aware_now(self._clock), payload={}, ttl_seconds=0, generation=request.generation)
            state["requests"][request.request_id] = replace(request, status="revoked", receipt_id=receipt.receipt_id).to_dict()
            return receipt
        current = self._receipt_from_dict(state["receipts"].get(request.receipt_id, {}))
        receipt = self._receipt_in_state(state, request, status="revoked", reason=reason, now=max(_aware_now(self._clock), parse_time(current.observed_at)), payload={}, ttl_seconds=0, generation=current.generation, receipt_id=current.receipt_id, replace_existing=True)
        state["requests"][request.request_id] = replace(request, status="revoked", receipt_id=receipt.receipt_id).to_dict()
        return receipt

    @staticmethod
    def _redacted_revoked_receipt(receipt: SourceReceipt) -> SourceReceipt:
        return replace(receipt, status="revoked", fresh_until=None, ttl_seconds=0, payload={}, reason="consent_revoked", payload_digest="")

    @staticmethod
    def _redacted_stale_receipt(receipt: SourceReceipt) -> SourceReceipt:
        return replace(receipt, status="stale", fresh_until=None, ttl_seconds=0, payload={}, reason="receipt_ttl_expired", payload_digest="")

    @staticmethod
    def _assert_scope(scope: tuple[str, str], *, user_id: str, session_id: str) -> None:
        provided = (str(user_id or ""), str(session_id or ""))
        if not all(provided) or provided != scope:
            raise SourceAdmissionError("request scope does not match the exact owner/session")

    @staticmethod
    def _assert_receipt_matches_request(receipt: SourceReceipt, request: SourceRequest) -> None:
        if receipt.request_id != request.request_id or receipt.need_id != request.need_id or receipt.scope != request.scope or receipt.source != request.source or receipt.binding_id != request.binding_id or receipt.generation != request.generation or receipt.consent_id != request.consent_id or receipt.consent_generation != request.consent_generation or receipt.consent_digest != request.consent_digest:
            raise SourceStateCorruptError("receipt/request binding is invalid")

    @staticmethod
    def _consent_matches(consent: SourceConsent | None, row: SourceRequest | SourceReceipt) -> bool:
        return bool(consent and row.consent_id == consent.consent_id and row.consent_generation == consent.generation and row.consent_digest == consent_digest_from_record(consent.to_dict()) and row.scope == consent.scope and row.source == consent.source)

    @staticmethod
    def _binding_from_dict(raw: Mapping[str, Any]) -> SourceNeedBinding:
        return binding_from_dict(raw)

    @staticmethod
    def _consent_from_dict(raw: Mapping[str, Any]) -> SourceConsent:
        return consent_from_dict(raw)

    @staticmethod
    def _request_from_dict(raw: Mapping[str, Any]) -> SourceRequest:
        return request_from_dict(raw)

    @staticmethod
    def _receipt_from_dict(raw: Mapping[str, Any]) -> SourceReceipt:
        return receipt_from_dict(raw)


__all__ = ["LivingSourceRuntime", "LivingSourceRuntimeError", "SourceAdmissionError", "SourceCapacityError", "SourceStateCorruptError"]
