from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.world_state import WorldStateStore
from interface.extension_spec import parse_extension_spec
from runtime.bounded_extension_generator import GeneratedExtensionSource
from runtime.extension_artifact_quarantine import ExtensionArtifactQuarantine
from runtime.extension_generation_gate import ExtensionGenerationGate
from runtime.extension_spec_quarantine import ExtensionSpecQuarantine
from scripts.phase6_extension_artifact_lifecycle_smoke import (
    BASE_TIME,
    USER,
    WORKSPACE,
    Clock,
    GatedContext,
    prepare_store,
)
from scripts.phase6_extension_spec_contract_smoke import valid_spec


CONTROL_TOKEN = "phase6-extension-generation-control-token"
SESSION = "phase6-extension-generation-session"
REQUEST = "phase6-extension-generation-request"
VALID_SOURCE = (
    b"def run_extension(payload):\n"
    b'    return {"label": payload["name"]}\n'
)


class FakeGenerator:
    def __init__(
        self,
        *,
        source: bytes = VALID_SOURCE,
        configured: bool = True,
        crash: BaseException | None = None,
    ) -> None:
        self.source = source
        self.configured = configured
        self.crash = crash
        self.calls = 0

    def identity(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "provider_id": "openai_compatible",
            "model_id": "test-model",
            "model_config_digest": "a" * 64,
        }

    def generate(self, **_: Any) -> GeneratedExtensionSource:
        self.calls += 1
        if self.crash is not None:
            raise self.crash
        return GeneratedExtensionSource(
            source_bytes=self.source,
            provider_id="openai_compatible",
            model_id="test-model",
            model_config_digest="a" * 64,
            duration_ms=7,
        )


@dataclass(slots=True)
class GenerationContext:
    temporary: TemporaryDirectory[str]
    root: Path
    store: WorldStateStore
    clock: Clock
    gated: GatedContext
    generator: FakeGenerator
    gate: ExtensionGenerationGate

    def close(self) -> None:
        self.temporary.cleanup()


def prepare_generation_context(
    *,
    generator: FakeGenerator | None = None,
    extension_id: str = "example.generated_projection",
    operation_prefix: str = "generation",
) -> GenerationContext:
    temporary = TemporaryDirectory()
    root = Path(temporary.name)
    store = prepare_store(root)
    clock = Clock(BASE_TIME)
    spec_runtime = ExtensionSpecQuarantine(
        state_store=store,
        now=clock,
    )
    artifact_runtime = ExtensionArtifactQuarantine(
        state_store=store,
        spec_quarantine=spec_runtime,
        now=clock,
    )
    spec_payload = valid_spec(now=clock.current)
    spec_payload["extension_id"] = extension_id
    spec_payload["dependencies"] = []
    spec = parse_extension_spec(spec_payload)
    quarantined = spec_runtime.quarantine(
        spec=spec,
        expected_spec_digest=spec.digest(),
        user_id=USER,
        workspace_id=WORKSPACE,
        operation_id=f"{operation_prefix}-spec-submit",
    )
    candidate = spec_runtime.review(
        candidate_id=str(quarantined["candidate_id"]),
        user_id=USER,
        workspace_id=WORKSPACE,
        expected_revision=1,
        operation_id=f"{operation_prefix}-spec-gate",
        decision="accept_for_future_isolated_generation",
        reason="Admit only bounded source generation into quarantine.",
    )
    subject = spec_runtime.artifact_subject(
        candidate_id=str(candidate["candidate_id"]),
        user_id=USER,
        workspace_id=WORKSPACE,
        require_gate_passed=True,
    )
    gated = GatedContext(
        store=store,
        clock=clock,
        spec_runtime=spec_runtime,
        artifact_runtime=artifact_runtime,
        candidate=candidate,
        subject=subject,
    )
    selected_generator = generator or FakeGenerator()
    gate = ExtensionGenerationGate(
        state_store=store,
        spec_quarantine=gated.spec_runtime,
        artifact_quarantine=gated.artifact_runtime,
        generator=selected_generator,  # type: ignore[arg-type]
        enabled=True,
        control_token=CONTROL_TOKEN,
        now=clock,
    )
    return GenerationContext(
        temporary=temporary,
        root=root,
        store=store,
        clock=clock,
        gated=gated,
        generator=selected_generator,
        gate=gate,
    )


def generation_command(
    context: GenerationContext,
    *,
    operation_id: str = "generation-start",
    session_id: str = SESSION,
    request_id: str = REQUEST,
    token: str = CONTROL_TOKEN,
) -> dict[str, Any]:
    candidate = context.gated.candidate
    return context.gate.generate(
        candidate_id=str(candidate["candidate_id"]),
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=session_id,
        request_id=request_id,
        operation_id=operation_id,
        expected_candidate_revision=int(candidate["candidate_revision"]),
        expected_spec_digest=str(candidate["spec_digest"]),
        control_token=token,
    )


def directory_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


__all__ = [
    "CONTROL_TOKEN",
    "FakeGenerator",
    "GenerationContext",
    "REQUEST",
    "SESSION",
    "USER",
    "VALID_SOURCE",
    "WORKSPACE",
    "directory_bytes",
    "generation_command",
    "prepare_generation_context",
]
