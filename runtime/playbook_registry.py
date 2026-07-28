from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,180}")
_ALLOWED_MODES = frozenset(
    {"disabled", "record_only", "shadow", "scoped_canary"}
)
_AUTONOMY_ORDER = {
    "A0": 0,
    "A1": 1,
    "A2": 2,
    "A3": 3,
}


class PlaybookRegistryError(RuntimeError):
    """A built-in playbook could not be selected or validated exactly."""


@dataclass(frozen=True, slots=True)
class PlaybookRegistration:
    """Trusted, immutable wiring for one built-in playbook implementation."""

    playbook_id: str
    version: int
    implementation_revision: str
    domain: str
    profile_id: str
    maximum_level: str
    risk_floor: str
    allowed_modes: tuple[str, ...]
    runner: Callable[[Any], dict[str, Any]]
    status_reader: Callable[[], dict[str, Any]]

    def public_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "version": self.version,
            "implementation_revision": self.implementation_revision,
            "domain": self.domain,
            "profile_id": self.profile_id,
            "maximum_level": self.maximum_level,
            "risk_floor": self.risk_floor,
            "allowed_modes": list(self.allowed_modes),
            "dynamic_registration": False,
        }


class PlaybookRegistry:
    """Exact dispatcher over application-assembled built-in playbooks.

    The registry deliberately has no ``register`` method and never loads a
    callable, spec, or authority profile from state, a model, or an Agent.
    """

    def __init__(
        self,
        registrations: Iterable[PlaybookRegistration],
    ) -> None:
        selected: dict[str, PlaybookRegistration] = {}
        for registration in registrations:
            self._validate_registration(registration)
            if registration.playbook_id in selected:
                raise ValueError(
                    f"duplicate playbook_id: {registration.playbook_id}"
                )
            selected[registration.playbook_id] = registration
        if not selected:
            raise ValueError("at least one built-in playbook is required")
        self._registrations: Mapping[str, PlaybookRegistration] = (
            MappingProxyType(selected)
        )

    def dispatch(
        self,
        *,
        playbook_id: str,
        version: int,
        implementation_revision: str,
        request: Any = None,
    ) -> dict[str, Any]:
        registration = self._resolve(
            playbook_id=playbook_id,
            version=version,
            implementation_revision=implementation_revision,
        )
        result = registration.runner(request)
        return self._validate_result(registration, result)

    def playbook_status(
        self,
        *,
        playbook_id: str,
        version: int,
        implementation_revision: str,
    ) -> dict[str, Any]:
        registration = self._resolve(
            playbook_id=playbook_id,
            version=version,
            implementation_revision=implementation_revision,
        )
        result = registration.status_reader()
        return self._validate_result(registration, result)

    def status(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for playbook_id, registration in sorted(
            self._registrations.items()
        ):
            try:
                current = self._validate_result(
                    registration,
                    registration.status_reader(),
                )
                items.append(
                    {
                        **registration.public_dict(),
                        "status": str(current.get("status") or "unknown"),
                        "mode": str(current.get("mode") or "unknown"),
                        "effective_autonomy_level": str(
                            current.get("effective_autonomy_level") or "A0"
                        ),
                    }
                )
            except Exception:
                items.append(
                    {
                        **registration.public_dict(),
                        "status": "fault",
                        "mode": "fail_closed",
                        "effective_autonomy_level": "A0",
                    }
                )
        return {
            "status": (
                "degraded"
                if any(item["status"] == "fault" for item in items)
                else "available"
            ),
            "registry_kind": "builtin_immutable",
            "dynamic_registration": False,
            "playbooks": items,
        }

    def catalog(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            registration.public_dict()
            for _, registration in sorted(self._registrations.items())
        )

    def _resolve(
        self,
        *,
        playbook_id: str,
        version: int,
        implementation_revision: str,
    ) -> PlaybookRegistration:
        normalized = str(playbook_id or "")
        registration = self._registrations.get(normalized)
        if registration is None:
            raise PlaybookRegistryError("unknown built-in playbook")
        if type(version) is not int or version != registration.version:
            raise PlaybookRegistryError("playbook version mismatch")
        if (
            str(implementation_revision or "")
            != registration.implementation_revision
        ):
            raise PlaybookRegistryError(
                "playbook implementation revision mismatch"
            )
        return registration

    @staticmethod
    def _validate_registration(
        registration: PlaybookRegistration,
    ) -> None:
        if not isinstance(registration, PlaybookRegistration):
            raise TypeError(
                "registrations must be PlaybookRegistration objects"
            )
        identifiers = {
            "playbook_id": registration.playbook_id,
            "implementation_revision": registration.implementation_revision,
            "domain": registration.domain,
            "profile_id": registration.profile_id,
        }
        for label, value in identifiers.items():
            if not _IDENTIFIER.fullmatch(str(value or "")):
                raise ValueError(f"{label} must be a bounded identifier")
        if type(registration.version) is not int or registration.version <= 0:
            raise ValueError("playbook version must be a positive integer")
        if registration.maximum_level not in {
            "A0",
            "A1",
            "A2",
            "A3",
        }:
            raise ValueError(
                "built-in registry cannot install A4/A5 authority"
            )
        if registration.risk_floor not in {
            "R0",
            "R1",
            "R2",
            "R3",
            "R4",
        }:
            raise ValueError(
                "playbook risk_floor is invalid or permanently blocked"
            )
        if (
            not registration.allowed_modes
            or len(set(registration.allowed_modes))
            != len(registration.allowed_modes)
            or not set(registration.allowed_modes).issubset(_ALLOWED_MODES)
        ):
            raise ValueError("playbook allowed_modes are invalid")
        if not callable(registration.runner) or not callable(
            registration.status_reader
        ):
            raise TypeError("playbook handlers must be callable")

    @staticmethod
    def _validate_result(
        registration: PlaybookRegistration,
        result: Any,
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise PlaybookRegistryError(
                "playbook handler returned a non-object result"
            )
        if result.get("playbook_id") != registration.playbook_id:
            raise PlaybookRegistryError(
                "playbook handler returned another playbook identity"
            )
        spec = result.get("spec")
        if not isinstance(spec, dict):
            raise PlaybookRegistryError(
                "playbook result lacks its exact registered spec"
            )
        if (
            spec.get("playbook_id") != registration.playbook_id
            or type(spec.get("version")) is not int
            or spec.get("version") != registration.version
            or spec.get("risk_floor") != registration.risk_floor
            or (
                "domain" in spec
                and spec.get("domain") != registration.domain
            )
            or (
                "maximum_level" in spec
                and spec.get("maximum_level")
                != registration.maximum_level
            )
            or (
                "implementation_revision" in spec
                and spec.get("implementation_revision")
                != registration.implementation_revision
            )
        ):
            raise PlaybookRegistryError(
                "playbook result spec differs from its registration"
            )
        effective_level = result.get("effective_autonomy_level")
        if (
            effective_level not in _AUTONOMY_ORDER
            or _AUTONOMY_ORDER[effective_level]
            > _AUTONOMY_ORDER[registration.maximum_level]
        ):
            raise PlaybookRegistryError(
                "playbook result exceeds its registered autonomy level"
            )
        profile = result.get("autonomy_profile")
        if profile is not None and (
            not isinstance(profile, dict)
            or profile.get("profile_id") != registration.profile_id
            or profile.get("domain") != registration.domain
            or profile.get("level") != registration.maximum_level
            or profile.get("global_authority") is not False
        ):
            raise PlaybookRegistryError(
                "playbook result autonomy profile differs from registration"
            )
        mode = str(result.get("mode") or "")
        if mode not in set(registration.allowed_modes) | {"fail_closed"}:
            raise PlaybookRegistryError(
                "playbook result contains an unregistered mode"
            )
        validated = dict(result)
        validated_spec = dict(spec)
        # Older built-ins did not repeat these immutable registry fields in
        # their local spec. Fill them from trusted registration metadata so a
        # registry result always exposes one exact, non-escalating identity.
        validated_spec.setdefault("domain", registration.domain)
        validated_spec.setdefault(
            "maximum_level",
            registration.maximum_level,
        )
        validated_spec.setdefault(
            "implementation_revision",
            registration.implementation_revision,
        )
        validated["spec"] = validated_spec
        return validated


__all__ = [
    "PlaybookRegistration",
    "PlaybookRegistry",
    "PlaybookRegistryError",
]
