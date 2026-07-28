#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.playbook_registry import (  # noqa: E402
    PlaybookRegistration,
    PlaybookRegistry,
    PlaybookRegistryError,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail!r}")
    print(f"ok - {label}")


def result(
    playbook_id: str = "test.read_only.v1",
    *,
    mode: str = "shadow",
    effective_level: str = "A1",
) -> dict[str, Any]:
    return {
        "playbook_id": playbook_id,
        "status": "healthy",
        "mode": mode,
        "effective_autonomy_level": effective_level,
        "spec": {
            "playbook_id": playbook_id,
            "version": 1,
            "domain": "test_domain",
            "maximum_level": "A1",
            "risk_floor": "R1",
            "implementation_revision": "test.read_only.impl.v1",
        },
        "autonomy_profile": {
            "profile_id": "aut.test.read_only.v1",
            "domain": "test_domain",
            "level": "A1",
            "global_authority": False,
        },
    }


def registration() -> PlaybookRegistration:
    return PlaybookRegistration(
        playbook_id="test.read_only.v1",
        version=1,
        implementation_revision="test.read_only.impl.v1",
        domain="test_domain",
        profile_id="aut.test.read_only.v1",
        maximum_level="A1",
        risk_floor="R1",
        allowed_modes=("disabled", "record_only", "shadow"),
        runner=lambda request: result(),
        status_reader=result,
    )


def denied(call: Any, label: str) -> None:
    try:
        call()
    except (PlaybookRegistryError, TypeError, ValueError):
        print(f"ok - {label}")
        return
    raise AssertionError(f"{label} failed: call unexpectedly succeeded")


def main() -> None:
    entry = registration()
    registry = PlaybookRegistry((entry,))
    dispatched = registry.dispatch(
        playbook_id=entry.playbook_id,
        version=entry.version,
        implementation_revision=entry.implementation_revision,
        request={"ignored": True},
    )
    expect(
        dispatched["playbook_id"] == entry.playbook_id,
        "exact built-in dispatch succeeds",
        dispatched,
    )
    expect(
        not hasattr(registry, "register"),
        "registry has no runtime registration method",
    )
    catalog = registry.catalog()
    expect(
        len(catalog) == 1
        and "runner" not in catalog[0]
        and "status_reader" not in catalog[0]
        and catalog[0]["dynamic_registration"] is False,
        "public catalog exposes metadata but no callables",
        catalog,
    )
    denied(
        lambda: registry.dispatch(
            playbook_id="test.unknown.v1",
            version=1,
            implementation_revision=entry.implementation_revision,
        ),
        "unknown playbook fails closed",
    )
    denied(
        lambda: registry.dispatch(
            playbook_id=entry.playbook_id,
            version=2,
            implementation_revision=entry.implementation_revision,
        ),
        "version drift fails closed",
    )
    denied(
        lambda: registry.dispatch(
            playbook_id=entry.playbook_id,
            version=1,
            implementation_revision="test.read_only.impl.v2",
        ),
        "implementation drift fails closed",
    )
    denied(
        lambda: PlaybookRegistry((entry, entry)),
        "duplicate built-in identity rejected",
    )
    denied(
        lambda: PlaybookRegistry(
            (replace(entry, maximum_level="A4"),)
        ),
        "registry cannot install A4 authority",
    )
    denied(
        lambda: PlaybookRegistry((replace(entry, risk_floor="R5"),)),
        "registry cannot install a permanently blocked R5 playbook",
    )
    wrong_identity = replace(
        entry,
        runner=lambda request: result("test.other.v1"),
    )
    wrong_registry = PlaybookRegistry((wrong_identity,))
    denied(
        lambda: wrong_registry.dispatch(
            playbook_id=entry.playbook_id,
            version=1,
            implementation_revision=entry.implementation_revision,
        ),
        "handler identity confusion fails closed",
    )
    wrong_mode = replace(
        entry,
        runner=lambda request: result(mode="scoped_canary"),
    )
    wrong_mode_registry = PlaybookRegistry((wrong_mode,))
    denied(
        lambda: wrong_mode_registry.dispatch(
            playbook_id=entry.playbook_id,
            version=1,
            implementation_revision=entry.implementation_revision,
        ),
        "unregistered handler mode fails closed",
    )
    elevated = replace(
        entry,
        runner=lambda request: result(effective_level="A2"),
    )
    elevated_registry = PlaybookRegistry((elevated,))
    denied(
        lambda: elevated_registry.dispatch(
            playbook_id=entry.playbook_id,
            version=1,
            implementation_revision=entry.implementation_revision,
        ),
        "handler cannot exceed registered autonomy level",
    )
    for field, value in (
        ("domain", "other_domain"),
        ("maximum_level", "A2"),
        ("risk_floor", "R0"),
        ("implementation_revision", "test.read_only.impl.v2"),
    ):
        forged = result()
        forged["spec"][field] = value
        forged_entry = replace(
            entry,
            runner=lambda request, forged=forged: forged,
        )
        forged_registry = PlaybookRegistry((forged_entry,))
        denied(
            lambda forged_registry=forged_registry: forged_registry.dispatch(
                playbook_id=entry.playbook_id,
                version=1,
                implementation_revision=entry.implementation_revision,
            ),
            f"spec {field} drift fails closed",
        )
    for field, value in (
        ("profile_id", "aut.other.v1"),
        ("domain", "other_domain"),
        ("level", "A2"),
        ("global_authority", True),
    ):
        forged = result()
        forged["autonomy_profile"][field] = value
        forged_entry = replace(
            entry,
            runner=lambda request, forged=forged: forged,
        )
        forged_registry = PlaybookRegistry((forged_entry,))
        denied(
            lambda forged_registry=forged_registry: forged_registry.dispatch(
                playbook_id=entry.playbook_id,
                version=1,
                implementation_revision=entry.implementation_revision,
            ),
            f"autonomy profile {field} drift fails closed",
        )
    print("playbook_registry_smoke: ok")


if __name__ == "__main__":
    main()
