#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.foresight_contract import (
    ForesightAssessment,
    ForesightContractError,
    UnknownCapabilityEffectContract,
    assess_tool_invocation,
    capability_effect_contracts,
)
from core.foresight_engine import ForesightEngine
from runtime.openclaw_tool_broker import (
    CUSTOM_TOOL_REGISTRY,
    HOOK_EXECUTOR_ID,
    HOOK_POLICY_REVISION,
    HOOK_REGISTRY_REVISION,
)
from tool_proxy.governance_contract import (
    GovernedSessionBinding,
    ToolInvocation,
    canonical_json,
    canonical_sha256,
)


NOW = datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)
EXECUTOR = HOOK_EXECUTOR_ID
POLICY_REVISION = HOOK_POLICY_REVISION
REGISTRY_REVISION = HOOK_REGISTRY_REVISION


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def binding(workspace: str) -> GovernedSessionBinding:
    return GovernedSessionBinding.create(
        user_id="local-user",
        workspace_id=workspace,
        agent_id="openclaw",
        session_id="session_foresight_contract",
        channel_id="api",
        case_id="case_foresight_contract",
        step_id="step_foresight_contract",
        run_id="run_foresight_contract",
    )


def invocation(
    workspace: str,
    *,
    tool_name: str = "file.write",
    tool_kind: str = "file",
    arguments: dict[str, Any] | None = None,
    targets: list[str] | None = None,
    environment_patch: dict[str, Any] | None = None,
    call_id: str = "call_foresight_contract",
) -> ToolInvocation:
    target = str(Path(workspace) / "effect.txt")
    environment = {
        "executor": EXECUTOR,
        "scope_digest": canonical_sha256({"sandbox_root": workspace}),
        "policy_revision": POLICY_REVISION,
        "registry_revision": REGISTRY_REVISION,
    }
    environment.update(environment_patch or {})
    return ToolInvocation.create(
        binding=binding(workspace),
        tool_call_id=call_id,
        tool_name=tool_name,
        tool_kind=tool_kind,
        arguments=arguments
        if arguments is not None
        else {"path": target, "content": "UNIQUE_RAW_CONTENT_MUST_NOT_PERSIST"},
        derived_targets=targets if targets is not None else [target],
        environment=environment,
        requested_at=NOW,
    )


def rejected(callback: Any, expected: type[Exception]) -> bool:
    try:
        callback()
    except expected:
        return True
    return False


def main() -> int:
    contracts = capability_effect_contracts()
    expect(
        len(contracts) == 6
        and {item.capability_id for item in contracts}
        == {
            "agent.status.read",
            "agent.capabilities.refresh",
            "agent.reconnect",
            "file.read",
            "file.write",
            "shell.run",
        },
        "registry contains only the six current fixed capabilities",
    )
    expect(
        all(item.revision == "veyra.fixed_capability_effects.v1" for item in contracts)
        and len({item.contract_digest for item in contracts}) == len(contracts),
        "every fixed capability has a versioned self-verifying contract",
    )
    tool_contracts = {
        item.capability_id: item
        for item in contracts
        if item.canonical_invocation_tool_name is not None
    }
    expect(
        {
            canonical_name: (
                contract.invocation_tool_kind,
                contract.risk_floor,
            )
            for canonical_name, contract in tool_contracts.items()
        }
        == {
            canonical_name: (tool_kind, risk)
            for _, (canonical_name, tool_kind, risk) in CUSTOM_TOOL_REGISTRY.items()
        }
        and all(
            contract.executor_id == HOOK_EXECUTOR_ID
            and contract.required_environment["policy_revision"]
            == HOOK_POLICY_REVISION
            and contract.required_environment["registry_revision"]
            == HOOK_REGISTRY_REVISION
            for contract in tool_contracts.values()
        ),
        "effect contracts match the current scoped broker registry and implementation identity",
    )
    contracts[0].required_environment["caller_mutation"] = "ignored"
    expect(
        "caller_mutation"
        not in capability_effect_contracts()[0].required_environment,
        "registry returns detached contracts instead of mutable global state",
    )

    with TemporaryDirectory(prefix="veyra-foresight-contract-") as workspace:
        call = invocation(workspace)
        assessment = assess_tool_invocation(call, created_at=NOW)
        serialized = canonical_json(assessment.model_dump(mode="json"))
        nodes = {node.node_id: node for node in assessment.effect_graph.nodes}
        edges = {
            (edge.source, edge.relation, edge.target)
            for edge in assessment.effect_graph.edges
        }
        target = str(Path(workspace) / "effect.txt")

        expect(
            assessment.invocation_digest == call.invocation_digest
            and assessment.args_digest == call.args_digest
            and assessment.targets_digest == call.targets_digest
            and assessment.environment_digest == call.environment_digest,
            "assessment binds every exact canonical invocation digest",
        )
        expect(
            nodes["invocation"].ref == call.invocation_digest
            and nodes["target_0"].ref == target
            and assessment.effect_graph.exact_target_count == 1
            and assessment.effect_graph.downstream_dependencies_unknown is True,
            "effect graph binds the invocation, exact target, and unknown downstream boundary",
        )
        expect(
            ("capability", "bound_to", "invocation") in edges
            and ("invocation", "writes", "target_0") in edges
            and ("target_0", "contained_by", "scope") in edges
            and ("capability", "verified_by", "verifier") in edges,
            "effect graph carries explicit capability, scope, write, and verifier edges",
        )
        expect(
            assessment.predicted_effect.authorized_targets == (target,)
            and assessment.predicted_effect.expected_changed_files == (target,)
            and assessment.rollback_mode == "disposable_sandbox"
            and assessment.trial_mode == "sandbox_trial"
            and assessment.risk_floor == "R2",
            "file.write prediction is exact, sandbox-only, reversible by disposal, and R2",
        )
        expect(
            "UNIQUE_RAW_CONTENT_MUST_NOT_PERSIST" not in serialized
            and target in serialized,
            "assessment persists target identity but never raw tool content",
        )

        engine_projection = ForesightEngine().assess_tool_invocation(
            call,
            created_at=NOW,
        )
        expect(
            engine_projection["assessment_digest"] == assessment.assessment_digest
            and len(ForesightEngine().effect_contracts()) == 6,
            "ForesightEngine exposes deterministic contracts without changing its old API",
        )

        mismatched_target = str(Path(workspace) / "other.txt")
        expect(
            rejected(
                lambda: assess_tool_invocation(
                    invocation(
                        workspace,
                        arguments={
                            "path": target,
                            "content": "content",
                        },
                        targets=[mismatched_target],
                        call_id="call_target_mismatch",
                    ),
                    created_at=NOW,
                ),
                ForesightContractError,
            ),
            "claimed targets cannot replace targets re-derived from exact parameters",
        )
        expect(
            rejected(
                lambda: assess_tool_invocation(
                    invocation(
                        workspace,
                        tool_name="veyra_file_write",
                        call_id="call_alias",
                    ),
                    created_at=NOW,
                ),
                ForesightContractError,
            )
            and rejected(
                lambda: assess_tool_invocation(
                    invocation(
                        workspace,
                        tool_kind="network",
                        call_id="call_kind",
                    ),
                    created_at=NOW,
                ),
                ForesightContractError,
            )
            and rejected(
                lambda: assess_tool_invocation(
                    invocation(
                        workspace,
                        environment_patch={"executor": "untrusted.executor"},
                        call_id="call_executor",
                    ),
                    created_at=NOW,
                ),
                ForesightContractError,
            ),
            "alias, tool-kind, and executor drift fail closed",
        )
        expect(
            rejected(
                lambda: assess_tool_invocation(
                    invocation(
                        workspace,
                        tool_name="unknown.tool",
                        call_id="call_unknown",
                    ),
                    created_at=NOW,
                ),
                UnknownCapabilityEffectContract,
            ),
            "unknown capability has no guessed effect contract",
        )

        tampered = assessment.model_dump(mode="json")
        tampered["effect_graph"]["nodes"][0]["ref"] = "tampered"
        expect(
            rejected(
                lambda: ForesightAssessment.model_validate_json(
                    canonical_json(tampered),
                    strict=True,
                ),
                ValidationError,
            ),
            "tampered effect graph fails strict integrity validation",
        )

    print("foresight effect contract smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
