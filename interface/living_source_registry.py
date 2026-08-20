"""Immutable server-side capability registry for Living Source providers."""

from __future__ import annotations

from common.living_source_primitives import SourceCapability


def capability_registry() -> dict[str, SourceCapability]:
    """Return the default registry; adapters are selected by the server."""

    return {
        "user_answer": SourceCapability(
            source="user_answer",
            provider_id="user_answer.pending.v1",
            description="A bounded user answer supplied through the Veyra conversation.",
            consent_required=False,
            default_ttl_seconds=86400,
            max_timeout_seconds=1.0,
            allowed_parameter_keys=(),
        ),
        "calendar": SourceCapability(
            source="calendar",
            provider_id="calendar.read_only.v1",
            description="Read-only events from an explicitly configured calendar source.",
            default_ttl_seconds=300,
            max_timeout_seconds=5.0,
            allowed_parameter_keys=("window_start", "window_end"),
        ),
        "weather": SourceCapability(
            source="weather",
            provider_id="weather.open_meteo.v1",
            description="Read-only weather observation for a server-bound place.",
            default_ttl_seconds=900,
            max_timeout_seconds=8.0,
            allowed_parameter_keys=("location",),
        ),
        "public_web": SourceCapability(
            source="public_web",
            provider_id="public_web.search_probe.v1",
            description="Read-only public web search through the configured SearchProbe.",
            default_ttl_seconds=1800,
            max_timeout_seconds=10.0,
            allowed_parameter_keys=("query", "max_results"),
        ),
        "agent_research": SourceCapability(
            source="agent_research",
            provider_id="agent_research.unavailable.v1",
            description="Reserved bounded research source; unavailable until separately governed.",
            enabled=False,
            default_ttl_seconds=900,
            max_timeout_seconds=1.0,
            allowed_parameter_keys=("topic",),
        ),
    }


__all__ = ["capability_registry"]
