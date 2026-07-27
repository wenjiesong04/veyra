from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlparse

from core.action_risk import (
    R0_COMMANDS,
    RISK_ORDER,
    assess_action_risk,
    assess_command_risk,
    assess_text_risk,
)
from tool_proxy.governance_contract import (
    RiskLevelValue,
    ToolInvocation,
    canonical_json,
)


_FILE_READ_TOOLS = frozenset(
    {
        "file.read",
        "file.read_text",
        "safe_file.read",
        "safe_file.read_text",
    }
)
_FILE_WRITE_TOOLS = frozenset(
    {
        "file.write",
        "file.write_text",
        "safe_file.write",
        "safe_file.write_text",
    }
)
_SHELL_TOOLS = frozenset(
    {
        "shell.exec",
        "shell.run",
        "safe_shell.exec",
        "safe_shell.run",
    }
)
_BROWSER_TOOLS = frozenset({"browser.open", "browser.navigate"})
_API_TOOLS = frozenset({"api.request", "http.request"})


def invocation_risk_floor(invocation: ToolInvocation) -> RiskLevelValue:
    """Return the deterministic minimum risk for an exact invocation.

    The Phase 3 contract-only issuer uses structured tool kind, arguments, and
    derived targets. Unknown tools fail to the existing R3 structured-action
    floor. The tool name is assessed independently so a misleading kind cannot
    hide an already-recognized destructive operation.
    """

    return _max_risk_levels(
        assess_text_risk(invocation.tool_name).risk_level,
        _structured_risk_floor(invocation),
    )


def _structured_risk_floor(invocation: ToolInvocation) -> RiskLevelValue:
    tool_name = invocation.tool_name
    tool_kind = invocation.tool_kind
    arguments = invocation.arguments

    required_kinds = {
        **{name: {"file"} for name in _FILE_READ_TOOLS | _FILE_WRITE_TOOLS},
        **{name: {"shell"} for name in _SHELL_TOOLS},
        **{name: {"browser"} for name in _BROWSER_TOOLS},
        **{name: {"api", "http"} for name in _API_TOOLS},
    }
    allowed_kinds = required_kinds.get(tool_name)
    if allowed_kinds is not None and tool_kind not in allowed_kinds:
        return "R5"

    if tool_name in _SHELL_TOOLS:
        command: Any = arguments.get("command")
        argv: Any = arguments.get("argv")
        # The contract-only slice has no SafeShell executor, so it accepts
        # only an unambiguous direct argv form for known side-effect-free
        # commands. Pipelines, wrappers, redirects, multi-command grammar,
        # and declared targets fail closed until the SafeShell slice.
        if (
            set(arguments) != {"argv"}
            or command is not None
            or argv is None
            or invocation.derived_targets
        ):
            return "R5"
        tokens = _strict_safe_shell_argv(argv)
        if tokens is None:
            return "R5"
        return cast(
            RiskLevelValue,
            assess_command_risk(tokens).risk_level,
        )

    if tool_name in _FILE_READ_TOOLS:
        if tool_kind != "file" or not _single_target_matches(
            arguments.get("path"),
            invocation.derived_targets,
        ):
            return "R5"
        return cast(
            RiskLevelValue,
            assess_action_risk(
                {"type": "file_read", "path": invocation.derived_targets[0]}
            ).risk_level,
        )
    if tool_name in _FILE_WRITE_TOOLS:
        if tool_kind != "file" or not _single_target_matches(
            arguments.get("path"),
            invocation.derived_targets,
        ):
            return "R5"
        return cast(
            RiskLevelValue,
            assess_action_risk(
                {"type": "file_write", "path": invocation.derived_targets[0]}
            ).risk_level,
        )
    if tool_name in _BROWSER_TOOLS:
        if not _single_target_matches(
            arguments.get("url"),
            invocation.derived_targets,
        ):
            return "R5"
        return cast(
            RiskLevelValue,
            assess_action_risk(
                {
                    "type": "browser_open",
                    "url": invocation.derived_targets[0],
                }
            ).risk_level,
        )
    if tool_name in _API_TOOLS:
        method = arguments.get("method")
        url_value = arguments.get("url")
        endpoint_value = arguments.get("endpoint")
        if url_value is not None and endpoint_value is not None:
            return "R5"
        endpoint = (
            url_value if url_value is not None else endpoint_value
        )
        if (
            not isinstance(method, str)
            or method != method.strip()
            or method.upper()
            not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
            or not _single_target_matches(
                endpoint,
                invocation.derived_targets,
            )
            or not _valid_http_url(endpoint)
        ):
            return "R5"
        return cast(
            RiskLevelValue,
            assess_action_risk(
                {
                    "type": "api_request",
                    "payload": {
                        "method": method.upper(),
                        "url": invocation.derived_targets[0],
                        # The existing API assessor now sees every structured
                        # field, including headers, query, and request body.
                        "body": arguments,
                    },
                }
            ).risk_level,
        )

    return _max_risk_levels(
        "R3",
        assess_text_risk(canonical_json(arguments)).risk_level,
        assess_text_risk(canonical_json(invocation.derived_targets)).risk_level,
    )


def _single_target_matches(value: Any, derived_targets: list[str]) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value == value.strip()
        and "\x00" not in value
        and derived_targets == [value]
    )


def _valid_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def _strict_safe_shell_argv(command: Any) -> list[str] | None:
    if not isinstance(command, list) or not command:
        return None
    if any(
        not isinstance(token, str)
        or not token
        or token != token.strip()
        or any(
            ord(character) < 32
            or ord(character) == 127
            or character in "|&;<>`$"
            for character in token
        )
        for token in command
    ):
        return None
    if command[0] not in R0_COMMANDS:
        return None
    return list(command)


def _max_risk_levels(*levels: str) -> RiskLevelValue:
    selected = "R0"
    for level in levels:
        if level not in RISK_ORDER:
            return "R5"
        if RISK_ORDER.index(level) > RISK_ORDER.index(selected):
            selected = level
    return cast(RiskLevelValue, selected)


__all__ = ["invocation_risk_floor"]
