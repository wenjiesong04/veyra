"""Generic server-owned source derivation for Living Context.

The policy translates an authoritative InformationNeed and Situation into a
bounded ``SourceNeedBinding``.  It deliberately knows nothing about a user's
life categories: source selection follows typed need/entity fields only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from interface.living_source_contract import (
    SourceNeedBinding,
    canonical_utc,
    make_binding_id,
)
from interface.living_source_registry import capability_registry
from interface.living_source_payload import weather_target


SOURCE_ORDER = ("calendar", "weather", "public_web")
MAX_WINDOW = timedelta(days=30)
DEFAULT_WINDOW = timedelta(days=7)
DEFAULT_BINDING_TTL = timedelta(hours=6)


class LivingContextSourcePolicy:
    """Pure source binding derivation; providers remain in LivingSourceRuntime."""

    def __init__(self, *, workspace_id: str = "server-derived") -> None:
        self.workspace_id = str(workspace_id or "server-derived")[:240]

    def choose_source(self, need: Mapping[str, Any]) -> str | None:
        allowed = {str(item).strip().lower() for item in need.get("allowed_source_classes", [])}
        evidence_kind = str(need.get("evidence_kind") or "").strip().lower()
        if evidence_kind in SOURCE_ORDER and evidence_kind in allowed:
            return evidence_kind
        for source in SOURCE_ORDER:
            if source in allowed:
                return source
        return None

    @staticmethod
    def watch_cadence_seconds(
        source: str,
        *,
        need: Mapping[str, Any] | None = None,
    ) -> int | None:
        """Return provider cadence after the typed refresh policy gate."""

        capability = capability_registry().get(str(source or "").strip())
        if capability is None or capability.watch_cadence_seconds is None:
            return None
        requirement = need.get("observation_requirement") if isinstance(need, Mapping) else None
        if isinstance(requirement, Mapping) and str(requirement.get("coverage") or "").strip().lower() == "current":
            # Current observations follow the provider freshness TTL; a
            # generic watch cadence is only a policy for repeated windows.
            return None
        target = need.get("evidence_target") if isinstance(need, Mapping) else None
        if str(source or "").strip().lower() == "weather" and isinstance(target, Mapping) and not str(target.get("target_date") or "").strip():
            return None
        return capability.watch_cadence_seconds

    def can_resolve_parameters(
        self,
        source: str,
        *,
        situation: Mapping[str, Any],
        need: Mapping[str, Any],
        now: datetime,
    ) -> bool:
        """Report whether a bounded request can be derived for *source*.

        This is the pure half of :meth:`derive_binding`: it answers whether the
        server owns enough typed context to build a request at all, without
        requiring the durable digest/generation that a real binding needs.
        """

        if not source:
            return False
        return self._parameters(source, situation=situation, need=need, now=self._aware(now)) is not None

    def derive_binding(
        self,
        *,
        situation: Mapping[str, Any],
        need: Mapping[str, Any],
        now: datetime,
    ) -> SourceNeedBinding | None:
        source = self.choose_source(need)
        if source is None:
            return None
        owner = str(need.get("owner_id") or "").strip()
        session = str(need.get("session_id") or "").strip()
        need_id = str(need.get("need_id") or "").strip()
        situation_id = str(need.get("situation_id") or situation.get("situation_id") or "").strip()
        digest = str(need.get("record_digest") or "").strip()
        generation = int(need.get("generation") or 0)
        if not owner or not session or not need_id or not situation_id or len(digest) != 64 or generation < 1:
            return None
        selected_now = self._aware(now)
        parameters = self._parameters(source, situation=situation, need=need, now=selected_now)
        if parameters is None:
            return None
        expires = self._binding_expiry(selected_now, situation=situation, need=need)
        return SourceNeedBinding(
            binding_id=make_binding_id(need_id, source, generation, digest),
            need_id=need_id,
            need_revision=generation,
            need_digest=digest,
            user_id=owner,
            workspace_id=self.workspace_id,
            session_id=session,
            situation_id=situation_id,
            source=source,  # type: ignore[arg-type]
            parameters=parameters,
            issued_at=canonical_utc(selected_now),
            expires_at=canonical_utc(expires),
        )

    def _parameters(
        self,
        source: str,
        *,
        situation: Mapping[str, Any],
        need: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any] | None:
        semantic = situation.get("semantic") if isinstance(situation.get("semantic"), Mapping) else situation
        if source == "calendar":
            end = self._future_bound(
                now,
                semantic.get("deadline_at"),
                need.get("expires_at"),
            )
            return {"window_start": canonical_utc(now), "window_end": canonical_utc(end)}
        if source == "weather":
            # New Needs carry a typed evidence_target.  It is the sole source
            # of truth for a forecast day; no free-text blocked judgment or
            # keyword/regex extraction is allowed to manufacture a target.
            raw_target = need.get("evidence_target")
            if isinstance(raw_target, Mapping):
                target = weather_target(
                    raw_target.get("location"),
                    raw_target.get("target_date"),
                )
                if target is not None:
                    selected = {"location": target["location"]}
                    if target.get("target_date"):
                        selected["target_date"] = target["target_date"]
                    return selected

            # A new readable Need without a server-derived typed target is
            # intentionally not source-readable.  Legacy rows remain visible
            # and can be migrated by a dedicated exact-state repair, but this
            # generic policy must not reconstruct a target from entities or
            # display prose at read time.
            return None
        if source == "public_web":
            title = " ".join(str(semantic.get("title") or semantic.get("label") or "").split())
            blocked = " ".join(str(need.get("blocked_judgment") or "").split())
            query = " ".join(item for item in (title, blocked) if item)[:320].strip()
            return {"query": query, "max_results": 5} if query else None
        # Agent research is intentionally not a source policy outcome in V1.
        return None

    @staticmethod
    def _place_source_parameter_is_eligible(
        entity: Mapping[str, Any],
    ) -> bool:
        """Accept reported spans or inferred model-attributed tentative input."""

        value = str(entity.get("value") or "").strip()
        if not value:
            return False
        scope = str(entity.get("provenance_scope") or "")
        if scope == "span":
            quote = entity.get("source_quote")
            return bool(
                str(entity.get("epistemic_status") or "").lower() == "reported"
                and isinstance(quote, Mapping)
                and quote.get("text") == entity.get("value")
                and type(quote.get("start")) is int
                and type(quote.get("end")) is int
                and quote.get("start", -1) >= 0
                and quote.get("end", 0) > quote.get("start", 0)
            )
        if scope == "model_attributed":
            # This is only a tentative parameter for a read-only, separately
            # consented weather lookup. It never becomes a reported fact or
            # grants authority to any other source/effect.
            return (
                str(entity.get("epistemic_status") or "").lower() == "inferred"
                and isinstance(entity.get("source_event_id"), str)
                and bool(str(entity.get("source_event_id") or "").strip())
                and entity.get("source_quote") is None
            )
        # Legacy bare reported entities have no admissible source provenance.
        return False

    @staticmethod
    def _future_bound(now: datetime, *values: Any) -> datetime:
        candidates: list[datetime] = []
        for value in values:
            if not value:
                continue
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                parsed = parsed.astimezone(timezone.utc)
                if parsed > now:
                    candidates.append(parsed)
        end = min(candidates) if candidates else now + DEFAULT_WINDOW
        end = min(end, now + MAX_WINDOW)
        return max(end, now + timedelta(minutes=5))

    @staticmethod
    def _binding_expiry(now: datetime, *, situation: Mapping[str, Any], need: Mapping[str, Any]) -> datetime:
        end = LivingContextSourcePolicy._future_bound(
            now,
            situation.get("deadline_at"),
            need.get("expires_at"),
        )
        if str(need.get("observation_mode") or "once").strip().lower() == "watch":
            # A provider horizon may be longer than its receipt cache TTL.
            # Keep a watch binding valid across that typed boundary; the
            # current Need generation/status remains the authority at read.
            return min(end, now + MAX_WINDOW)
        return min(end, now + DEFAULT_BINDING_TTL)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source policy clock must be timezone-aware")
        return value.astimezone(timezone.utc)


__all__ = ["LivingContextSourcePolicy"]
