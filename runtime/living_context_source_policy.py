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
            entities = semantic.get("entities") if isinstance(semantic.get("entities"), list) else []
            for entity in entities:
                if not isinstance(entity, Mapping):
                    continue
                if str(entity.get("kind") or "").lower() != "place":
                    continue
                # An inferred place is not an external source target.
                if str(entity.get("epistemic_status") or "reported").lower() != "reported":
                    continue
                value = " ".join(str(entity.get("value") or "").split())[:160]
                if value:
                    return {"location": value}
            return None
        if source == "public_web":
            title = " ".join(str(semantic.get("title") or semantic.get("label") or "").split())
            blocked = " ".join(str(need.get("blocked_judgment") or "").split())
            query = " ".join(item for item in (title, blocked) if item)[:320].strip()
            return {"query": query, "max_results": 5} if query else None
        # Agent research is intentionally not a source policy outcome in V1.
        return None

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
        return min(end, now + DEFAULT_BINDING_TTL)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source policy clock must be timezone-aware")
        return value.astimezone(timezone.utc)


__all__ = ["LivingContextSourcePolicy"]
