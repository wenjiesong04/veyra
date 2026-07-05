from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


RISK_ORDER: tuple[str, ...] = ("R0", "R1", "R2", "R3", "R4", "R5")


@dataclass(frozen=True, slots=True)
class ActionRiskAssessment:
    risk_level: str
    reason: str
    category: str
    signals: tuple[str, ...] = ()
    normalized_action: str = ""
    confidence: float = 0.78

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["signals"] = list(self.signals)
        return data


def assess_text_risk(text: str) -> ActionRiskAssessment:
    raw = str(text or "")
    lowered = raw.lower()
    assessment = _baseline("no side-effecting action detected", raw)
    assessment = _max_assessment(assessment, _assess_shell_text(lowered, raw))
    assessment = _max_assessment(assessment, _assess_natural_language(lowered, raw))
    return assessment


def assess_command_risk(command: Sequence[Any] | str) -> ActionRiskAssessment:
    tokens = _command_tokens(command)
    normalized = " ".join(tokens)
    if not tokens:
        return _assessment("R3", "empty or unparsable shell command requires review", "unknown_shell", ("shell:unparsed",), normalized, 0.62)

    shell_script = _embedded_shell_script(tokens)
    if shell_script:
        nested = assess_command_risk(shell_script)
        return _assessment(
            nested.risk_level,
            f"embedded shell command: {nested.reason}",
            nested.category,
            ("shell:embedded", *nested.signals),
            normalized,
            nested.confidence,
        )

    lowered = " ".join(token.lower() for token in tokens)
    executable = _command_executable(tokens)
    assessment = _assess_shell_text(lowered, normalized)
    if assessment.risk_level != "R0":
        return assessment

    if executable == "git":
        return _assess_git_command(tokens, normalized)
    if executable == "find":
        return _assess_find_command(tokens, normalized)
    if executable in READ_ONLY_COMMANDS:
        return _assessment("R1", "recognized read-only shell command", "read_only_shell", (f"cmd:{executable}",), normalized, 0.86)
    if executable in R0_COMMANDS:
        return _assessment("R0", "side-effect-free shell command", "safe_shell", (f"cmd:{executable}",), normalized, 0.84)
    if executable in LOW_WRITE_COMMANDS:
        return _assessment("R2", "scoped local write command requires trace evidence", "local_write_shell", (f"cmd:{executable}",), normalized, 0.76)
    if executable:
        return _assessment("R3", "unknown shell command requires review before execution", "unknown_shell", (f"cmd:{executable}",), normalized, 0.64)
    return _assessment("R3", "unrecognized shell command requires review", "unknown_shell", ("shell:unrecognized",), normalized, 0.62)


def assess_action_risk(action: Mapping[str, Any]) -> ActionRiskAssessment:
    action_type = str(action.get("type") or "").strip()
    if action_type == "shell_command":
        return assess_command_risk(action.get("command") or "")
    if action_type == "file_read":
        path = str(action.get("path") or "")
        if _is_sensitive_path(path):
            return _assessment("R5", "sensitive file read is blocked", "sensitive_file_read", ("file:sensitive",), path, 0.9)
        return _assessment("R1", "file read is read-only", "file_read", ("file:read",), path, 0.86)
    if action_type == "file_write":
        path = str(action.get("path") or "")
        if _is_sensitive_path(path):
            return _assessment("R4", "sensitive file write requires confirmation", "sensitive_file_write", ("file:sensitive", "file:write"), path, 0.88)
        return _assessment("R2", "file write requires scoped diff and snapshot", "file_write", ("file:write",), path, 0.84)
    if action_type == "browser_open":
        return _assess_browser(str(action.get("url") or ""))
    if action_type == "api_request":
        payload = action.get("payload") if isinstance(action.get("payload"), Mapping) else {}
        return _assess_api(payload)
    if action_type == "rollback_restore":
        return _assessment("R4", "rollback restore requires confirmation", "rollback_restore", ("rollback:restore",), str(action.get("snapshot_id") or ""), 0.9)
    return _assessment("R3", "unknown structured action requires review", "unknown_action", (f"action:{action_type or 'missing'}",), str(dict(action)), 0.62)


def max_risk(left: str, right: str) -> str:
    return right if RISK_ORDER.index(right) > RISK_ORDER.index(left) else left


R0_COMMANDS = {
    "echo",
    "printf",
    "true",
    "false",
}

READ_ONLY_COMMANDS = {
    "awk",
    "cat",
    "date",
    "df",
    "du",
    "env",
    "git",
    "grep",
    "head",
    "id",
    "jq",
    "less",
    "lsof",
    "ls",
    "netstat",
    "pgrep",
    "ps",
    "pwd",
    "rg",
    "sed",
    "stat",
    "tail",
    "uname",
    "wc",
    "which",
}

LOW_WRITE_COMMANDS = {
    "git",
    "mkdir",
    "touch",
}


def _assess_natural_language(lowered: str, raw: str) -> ActionRiskAssessment:
    checks: tuple[tuple[str, str, str, tuple[str, ...], float], ...] = (
        ("R5", r"\brm\s+-rf\b|\bcurl\b.*\|\s*\bbash\b|\bdrop\s+(database|table)\b|\btruncate\s+table\b|\bgit\s+push\s+(-f|--force)\b", "forbidden destructive action", ("text:forbidden",), 0.9),
        ("R5", r"\.env\s*(外发|上传|发送|upload|send|post)|窃取.*密码|盗取.*账号|绕过.*检测|钓鱼邮件?|phishing|勒索软件", "credential exfiltration or abuse request", ("text:abuse",), 0.9),
        ("R4", r"\bsudo\b|\brestart\b|\brollback\b|\brestore\b|\bdeploy\b|\bdelete\b|\bgit\s+reset\s+(--|–|—)?hard\b|\bchmod\s+-r\b|\bchown\s+-r\b", "high-impact operation requires review", ("text:high_impact",), 0.82),
        ("R4", r"重启|回滚|恢复快照|部署|删除|生产|付费\s*api|停止.*服务|启动.*服务", "high-impact operation requires review", ("text:high_impact",), 0.82),
        ("R3", r"修改配置|覆盖|迁移|数据库|批量|\bconfig\b|\boverwrite\b|\bmigration\b", "stateful or broad change requires review", ("text:medium_change",), 0.74),
        ("R2", r"写入|创建文件|修改文件|修改.*代码|实现.*功能|开发|\bcommit\b|提交|\bwrite\b", "scoped write requires trace evidence", ("text:write",), 0.72),
        ("R1", r"检查|查看|读取|状态|端口|进程|\bcheck\b|\bstatus\b|\bread\b|\bport\b|\bprocess\b", "read-only request", ("text:read_only",), 0.72),
    )
    for risk, pattern, reason, signals, confidence in checks:
        if re.search(pattern, lowered):
            return _assessment(risk, reason, "natural_language", signals, raw, confidence)
    return _baseline("no side-effecting natural language action detected", raw)


def _assess_shell_text(lowered: str, raw: str) -> ActionRiskAssessment:
    high_confidence_checks: tuple[tuple[str, str, str, tuple[str, ...], str], ...] = (
        ("R5", r"\brm\s+[^|;&]*-[^\s]*r[^\s]*f|\brm\s+[^|;&]*-[^\s]*f[^\s]*r", "recursive force delete is blocked", ("shell:rm_rf",), "destructive_shell"),
        ("R5", r"\b(curl|wget)\b[^|;&]*\|\s*(bash|sh|zsh)\b", "download-and-execute shell pipeline is blocked", ("shell:download_pipe_exec",), "download_exec"),
        ("R5", r"\bgit\s+push\b[^|;&]*(--force|-f)\b|\bgit\s+reset\b[^|;&]*--hard\b|\bgit\s+clean\b[^|;&]*-[^\s]*f[^\s]*d", "destructive git operation is blocked", ("shell:git_destructive",), "git_destructive"),
        ("R5", r"\b(drop\s+database|drop\s+table|truncate\s+table)\b", "destructive database operation is blocked", ("shell:database_destructive",), "database_destructive"),
        ("R5", r"\b(find\b[^|;&]*\s-delete|shred\b|mkfs(\.\w+)?\b|diskutil\b[^|;&]*\berase|dd\b[^|;&]*\bof=/dev/)", "destructive filesystem operation is blocked", ("shell:filesystem_destructive",), "filesystem_destructive"),
        ("R5", r"\.env\b.*\b(curl|wget|scp|rsync|nc|netcat|upload|send|post)\b", "sensitive file exfiltration is blocked", ("shell:secret_exfiltration",), "secret_exfiltration"),
        ("R4", r"\bsudo\b", "sudo command requires confirmation", ("shell:sudo",), "privileged_shell"),
        ("R4", r"\blaunchctl\b[^|;&]*\b(kickstart|bootstrap|bootout|load|unload|remove|enable|disable)\b", "launchctl service control requires confirmation", ("shell:service_control", "cmd:launchctl"), "service_control"),
        ("R4", r"\bsystemctl\b[^|;&]*\b(start|stop|restart|reload|enable|disable|kill)\b", "systemctl service control requires confirmation", ("shell:service_control", "cmd:systemctl"), "service_control"),
        ("R4", r"\bservice\b\s+\S+\s+(start|stop|restart|reload)\b", "service control requires confirmation", ("shell:service_control", "cmd:service"), "service_control"),
        ("R4", r"\bbrew\s+services\s+(start|stop|restart|run|kill)\b", "brew service control requires confirmation", ("shell:service_control", "cmd:brew"), "service_control"),
        ("R4", r"\b(pm2|supervisorctl)\b[^|;&]*\b(start|stop|restart|reload|delete)\b", "process manager control requires confirmation", ("shell:service_control",), "service_control"),
        ("R4", r"\b(kill|pkill|killall)\b", "process termination requires confirmation", ("shell:process_control",), "process_control"),
        ("R4", r"\bdocker(\s+compose)?\b[^|;&]*\b(up|down|restart|stop|rm|kill)\b|\bkubectl\b[^|;&]*\b(apply|delete|rollout\s+restart|scale)\b", "container or cluster state change requires confirmation", ("shell:runtime_control",), "runtime_control"),
        ("R4", r"\brm\b|\bdelete\b|\bchmod\s+-r\b|\bchown\s+-r\b", "delete or broad permission change requires confirmation", ("shell:high_impact",), "high_impact_shell"),
        ("R3", r"\bsed\b[^|;&]*\s-i\b|\btee\b|\b(npm|pnpm|yarn|pip|uv|brew)\b[^|;&]*\b(install|add|remove|uninstall|upgrade|update)\b", "state-changing command requires review", ("shell:state_change",), "state_change_shell"),
        ("R2", r"\bgit\s+(add|commit|tag)\b|\bmkdir\b|\btouch\b|>\s*\S+|>>\s*\S+", "scoped local write requires trace evidence", ("shell:low_write",), "local_write_shell"),
    )
    for risk, pattern, reason, signals, category in high_confidence_checks:
        if re.search(pattern, lowered):
            return _assessment(risk, reason, category, signals, raw, 0.88 if risk in {"R4", "R5"} else 0.76)
    return _baseline("no shell risk marker detected", raw)


def _assess_browser(url: str) -> ActionRiskAssessment:
    parsed = urlparse(url)
    if parsed.scheme in {"javascript", "data"}:
        return _assessment("R5", "unsafe browser URL scheme is blocked", "browser_open", ("browser:unsafe_scheme",), url, 0.9)
    if parsed.scheme == "file":
        return _assessment("R4", "local file browser access requires confirmation", "browser_open", ("browser:file_url",), url, 0.86)
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return _assessment("R1", "local browser target is read-only", "browser_open", ("browser:local",), url, 0.8)
    return _assessment("R2", "external browser target is allowed with constraints", "browser_open", ("browser:external",), url, 0.7)


def _assess_api(payload: Mapping[str, Any]) -> ActionRiskAssessment:
    method = str(payload.get("method", "GET")).upper()
    url = str(payload.get("url") or payload.get("endpoint") or "")
    body = payload.get("body") or payload.get("json") or {}
    text = f"{method} {url} {body}".lower()
    if any(token in text for token in ("api_key", "authorization", "bearer ", "secret", ".env")):
        return _assessment("R5", "API request appears to contain sensitive material", "api_request", ("api:sensitive",), f"{method} {url}", 0.9)
    if method in {"DELETE", "PATCH", "PUT", "POST"}:
        return _assessment("R4", "state-changing API request requires confirmation", "api_request", ("api:state_change",), f"{method} {url}", 0.86)
    return _assessment("R1", "read-only API request", "api_request", ("api:read_only",), f"{method} {url}", 0.82)


def _command_tokens(command: Sequence[Any] | str) -> list[str]:
    if isinstance(command, str):
        try:
            return [token for token in shlex.split(command) if token]
        except ValueError:
            return [part for part in command.split() if part]
    return [str(part) for part in command if str(part)]


def _embedded_shell_script(tokens: list[str]) -> str:
    executable = _command_executable(tokens)
    if executable not in {"bash", "sh", "zsh", "fish"}:
        return ""
    lowered = [token.lower() for token in tokens]
    if "-c" not in lowered:
        return ""
    index = lowered.index("-c")
    return str(tokens[index + 1]) if index + 1 < len(tokens) else ""


def _command_executable(tokens: list[str]) -> str:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        base = Path(token).name.lower()
        if base == "sudo":
            index += 1
            continue
        if base == "env":
            index += 1
            while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith("-"):
                index += 1
            if index >= len(tokens):
                return "env"
            continue
        return base
    return ""


def _assess_git_command(tokens: list[str], normalized: str) -> ActionRiskAssessment:
    subcommand = ""
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        subcommand = token.lower()
        break
    readonly = {"status", "diff", "log", "show", "rev-parse", "remote", "branch", "ls-files", "describe"}
    scoped_write = {"add", "commit", "tag", "stash"}
    review_required = {"checkout", "switch", "merge", "rebase", "pull", "restore", "revert", "reset", "push"}
    if subcommand in readonly:
        if subcommand == "branch" and any(flag in {"-d", "-D", "--delete"} for flag in tokens):
            return _assessment("R4", "git branch deletion requires confirmation", "git_state_change", ("shell:git_state_change", "cmd:git"), normalized, 0.86)
        return _assessment("R1", "recognized read-only git command", "read_only_shell", ("shell:git_read", "cmd:git"), normalized, 0.86)
    if subcommand in scoped_write:
        return _assessment("R2", "scoped git state change requires trace evidence", "git_state_change", ("shell:git_state_change", "cmd:git"), normalized, 0.78)
    if subcommand in review_required:
        return _assessment("R4", "git history or workspace state change requires confirmation", "git_state_change", ("shell:git_state_change", "cmd:git"), normalized, 0.84)
    return _assessment("R3", "unknown git command requires review", "git_unknown", ("shell:git_unknown", "cmd:git"), normalized, 0.68)


def _assess_find_command(tokens: list[str], normalized: str) -> ActionRiskAssessment:
    side_effect_flags = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
    if any(token.lower() in side_effect_flags for token in tokens):
        return _assessment("R4", "find command with executable side effects requires confirmation", "filesystem_state_change", ("shell:find_side_effect", "cmd:find"), normalized, 0.86)
    return _assessment("R1", "find command without side-effect flags is read-only", "read_only_shell", ("cmd:find",), normalized, 0.82)


def _is_sensitive_path(path: str) -> bool:
    target = Path(path)
    sensitive_names = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
    return target.name in sensitive_names or any(part in {".ssh", ".gnupg"} for part in target.parts)


def _max_assessment(left: ActionRiskAssessment, right: ActionRiskAssessment) -> ActionRiskAssessment:
    if RISK_ORDER.index(right.risk_level) > RISK_ORDER.index(left.risk_level):
        return right
    return left


def _baseline(reason: str, normalized: str) -> ActionRiskAssessment:
    return _assessment("R0", reason, "none", (), normalized, 0.55)


def _assessment(
    risk_level: str,
    reason: str,
    category: str,
    signals: tuple[str, ...],
    normalized_action: str,
    confidence: float,
) -> ActionRiskAssessment:
    return ActionRiskAssessment(
        risk_level=risk_level,
        reason=reason,
        category=category,
        signals=signals,
        normalized_action=normalized_action,
        confidence=confidence,
    )
