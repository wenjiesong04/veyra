"""Dependency composition for the V1 Living Context vertical slice."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sys
from typing import Any, Callable, Mapping

from core.world_state import WorldStateStore
from runtime.calendar_source import CalendarSource, DisabledCalendarProvider, MacOSCalendarProvider
from runtime.information_need_runtime import InformationNeedRuntime
from runtime.living_context_orchestrator import LivingContextOrchestrator
from runtime.living_context_runtime import LivingContextRuntime
from runtime.living_context_source_policy import LivingContextSourcePolicy
from runtime.living_reaction_runtime import LivingReactionRuntime
from runtime.living_source_runtime import LivingSourceRuntime


@dataclass(slots=True)
class LivingContextComposition:
    core: LivingContextRuntime
    needs: InformationNeedRuntime
    reaction: LivingReactionRuntime
    source: LivingSourceRuntime
    policy: LivingContextSourcePolicy
    orchestrator: LivingContextOrchestrator


def build_living_context_composition(
    state_store: WorldStateStore,
    *,
    weather_probe: Any | None = None,
    search_probe: Any | None = None,
    calendar_source: CalendarSource | None = None,
    clock: Callable[[], datetime] | None = None,
    source_policy: LivingContextSourcePolicy | None = None,
    source_providers: Mapping[str, Any] | None = None,
    conversation_runtime: Any | None = None,
) -> LivingContextComposition:
    """Construct core/reaction/source once and expose only the facade."""

    selected_clock = clock or (lambda: datetime.now().astimezone())
    needs = InformationNeedRuntime(state_store, clock=selected_clock)
    core = LivingContextRuntime(state_store, information_need_runtime=needs, clock=selected_clock)
    reaction = LivingReactionRuntime(state_store, clock=selected_clock)
    if calendar_source is None:
        # On macOS the built-in adapter is configured by the product bundle,
        # but it starts with system_permission=unknown.  That keeps GET
        # /sources pure and available=false until the user consents and a
        # controlled read establishes TCC access.  Non-macOS deployments do
        # not have an implicit Calendar provider and remain not_configured.
        provider = MacOSCalendarProvider(enabled=True) if sys.platform == "darwin" else DisabledCalendarProvider()
        calendar_source = CalendarSource(provider)
    source = LivingSourceRuntime(
        state_store,
        current_need_resolver=lambda owner, session, need_id: needs.authoritative_projection(
            need_id,
            owner_id=owner,
            session_id=session,
        ),
        weather_probe=weather_probe,
        search_probe=search_probe,
        calendar_source=calendar_source,
        providers=source_providers,
        clock=selected_clock,
    )
    policy = source_policy or LivingContextSourcePolicy()
    orchestrator = LivingContextOrchestrator(
        core,
        reaction,
        source,
        source_policy=policy,
        clock=selected_clock,
        conversation_runtime=conversation_runtime,
    )
    return LivingContextComposition(
        core=core,
        needs=needs,
        reaction=reaction,
        source=source,
        policy=policy,
        orchestrator=orchestrator,
    )


__all__ = ["LivingContextComposition", "build_living_context_composition"]
