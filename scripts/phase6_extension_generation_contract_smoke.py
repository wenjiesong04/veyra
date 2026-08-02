#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any, Callable

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.model_client import CoreModelConfig  # noqa: E402
from interface.extension_artifact import artifact_owner_scope_digest  # noqa: E402
from interface.extension_generation import (  # noqa: E402
    EXTENSION_GENERATION_BINDING_SCHEMA_VERSION,
    EXTENSION_GENERATION_POLICY_DIGEST,
    EXTENSION_GENERATION_POLICY_REVISION,
    EXTENSION_GENERATION_PROMPT_DIGEST,
    EXTENSION_GENERATION_REPORT_SCHEMA_VERSION,
    EXTENSION_GENERATOR_REVISION,
    ExtensionGenerationAuthority,
    ExtensionGenerationBinding,
    ExtensionGenerationReport,
    authenticated_local_principal_digest,
    initiating_session_digest,
)
from interface.extension_spec import (  # noqa: E402
    EXTENSION_POLICY_REVISION,
    parse_extension_spec,
)
from interface.extension_artifact import (  # noqa: E402
    EXTENSION_ARTIFACT_POLICY_REVISION,
)
from runtime.bounded_extension_generator import (  # noqa: E402
    BoundedExtensionGenerator,
    BoundedExtensionGeneratorOutputError,
)
from scripts.phase6_extension_spec_contract_smoke import valid_spec  # noqa: E402


NOW = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_raises(
    errors: tuple[type[BaseException], ...],
    call: Callable[[], Any],
    label: str,
) -> None:
    try:
        call()
    except errors:
        print(f"PASS {label}")
        return
    raise AssertionError(f"{label}: call did not fail closed")


class FakeCoreModelClient:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def config(self) -> CoreModelConfig:
        return CoreModelConfig(
            enabled=True,
            provider="openai_compatible",
            base_url="https://model.example.invalid/v1",
            api_key="test-key",
            model="test-model",
            timeout=5.0,
            max_tokens=1600,
        )

    @staticmethod
    def _api_key_required(_: CoreModelConfig) -> bool:
        return True

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return dict(self.result)


def spec() -> Any:
    payload = valid_spec(now=NOW)
    payload["dependencies"] = []
    return parse_extension_spec(payload)


def binding(generator: BoundedExtensionGenerator) -> ExtensionGenerationBinding:
    selected_spec = spec()
    identity = generator.identity()
    return ExtensionGenerationBinding(
        schema_version=EXTENSION_GENERATION_BINDING_SCHEMA_VERSION,
        generation_id="extgen_" + "1" * 24,
        candidate_id="extspec_" + "2" * 24,
        candidate_revision=2,
        owner_scope_digest=artifact_owner_scope_digest(
            "generation-user",
            "/private/generation-workspace",
        ),
        authenticated_principal_digest=(
            authenticated_local_principal_digest(
                "phase6-extension-generation-control-token"
            )
        ),
        initiating_session_digest=initiating_session_digest(
            "generation-session"
        ),
        request_provenance_digest="3" * 64,
        extension_id=selected_spec.extension_id,
        extension_version=selected_spec.version,
        spec_digest=selected_spec.digest(),
        candidate_expires_at=(NOW + timedelta(days=7)).isoformat(),
        extension_policy_revision=EXTENSION_POLICY_REVISION,
        artifact_policy_revision=EXTENSION_ARTIFACT_POLICY_REVISION,
        generation_policy_revision=EXTENSION_GENERATION_POLICY_REVISION,
        generation_policy_digest=EXTENSION_GENERATION_POLICY_DIGEST,
        generator_revision=EXTENSION_GENERATOR_REVISION,
        prompt_digest=EXTENSION_GENERATION_PROMPT_DIGEST,
        provider_id=str(identity["provider_id"]),
        model_id=str(identity["model_id"]),
        model_config_digest=str(identity["model_config_digest"]),
    )


def main() -> int:
    result = {
        "status": "model_assisted",
        "model_status": "ok",
        "source": (
            "def run_extension(payload):\n"
            '    return {"label": payload["name"]}\n'
        ),
        "duration_ms": 8,
        "_model": {
            "provider": "openai_compatible",
            "model": "test-model",
            "purpose": "phase6_extension_generation",
        },
    }
    client = FakeCoreModelClient(result)
    generator = BoundedExtensionGenerator(client)  # type: ignore[arg-type]
    selected_binding = binding(generator)
    generated = generator.generate(
        binding=selected_binding,
        spec=spec(),
    )
    expect(
        generated.source_bytes == result["source"].encode("utf-8")
        and len(client.calls) == 1
        and client.calls[0]["purpose"]
        == "phase6_extension_generation",
        "bounded generator returns canonical source data only",
    )
    expect(
        set(client.calls[0]) == {"purpose", "system", "user"}
        and "/private/generation-workspace" not in client.calls[0]["user"]
        and "phase6-extension-generation-control-token"
        not in client.calls[0]["user"],
        "model request contains no workspace principal session secret or tool input",
        client.calls[0],
    )
    expect(
        selected_binding.binding_digest()
        == ExtensionGenerationBinding.model_validate(
            selected_binding.model_dump(mode="python"),
            strict=True,
        ).binding_digest(),
        "generation binding has stable canonical identity",
    )
    expect(
        ExtensionGenerationAuthority().code_generation is True
        and ExtensionGenerationAuthority().private_artifact_quarantine_write
        is True
        and not any(
            value
            for key, value in ExtensionGenerationAuthority()
            .model_dump(mode="python")
            .items()
            if key
            not in {
                "code_generation",
                "private_artifact_quarantine_write",
            }
        ),
        "generation authority is limited to model output and private quarantine",
    )

    report = ExtensionGenerationReport(
        schema_version=EXTENSION_GENERATION_REPORT_SCHEMA_VERSION,
        binding=selected_binding,
        binding_digest=selected_binding.binding_digest(),
        generation_status="quarantined",
        artifact_id="extart_" + "4" * 24,
        artifact_sha256="5" * 64,
        artifact_size_bytes=len(generated.source_bytes),
        artifact_envelope_digest="6" * 64,
        generated_at=NOW.isoformat(),
    )
    expect(
        report.report_digest()
        == ExtensionGenerationReport.model_validate(
            report.model_dump(mode="python"),
            strict=True,
        ).report_digest(),
        "source-free terminal report has stable canonical identity",
    )
    expect_raises(
        (ValidationError,),
        lambda: ExtensionGenerationReport.model_validate(
            {
                **report.model_dump(mode="python"),
                "failure_code": "model_output_invalid",
            },
            strict=True,
        ),
        "successful report cannot also claim a failure",
    )

    client.result = {
        **result,
        "source": "def run_extension(payload):\n    return {}",
    }
    kimi_style = generator.generate(
        binding=selected_binding,
        spec=spec(),
    )
    expect(
        kimi_style.source_bytes
        == b"def run_extension(payload):\n    return {}\n",
        "missing terminal LF is normalized across compatible model transports",
    )

    for label, mutated in (
        (
            "model response with extra authority field is rejected",
            {**result, "installed": True},
        ),
        (
            "model response with wrong provider identity is rejected",
            {
                **result,
                "_model": {
                    **result["_model"],
                    "provider": "another-provider",
                },
            },
        ),
    ):
        failing = BoundedExtensionGenerator(
            FakeCoreModelClient(mutated)  # type: ignore[arg-type]
        )
        expect_raises(
            (BoundedExtensionGeneratorOutputError,),
            lambda failing=failing: failing.generate(
                binding=binding(failing),
                spec=spec(),
            ),
            label,
        )

    print("Phase 6 extension generation contract smoke passed: 9/9")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
