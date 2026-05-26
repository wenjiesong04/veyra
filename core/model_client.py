from __future__ import annotations

import json
import os
import re
import ssl
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.world_state import WorldStateStore


SENSITIVE_KEY_MARKERS = ("api_key", "apikey", "token", "secret", "password", "private_key", "authorization")


@dataclass(slots=True)
class CoreModelConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = "VEYRA_CORE_MODEL_API_KEY"
    model: str = ""
    timeout: float = 20.0
    decision_mode: str = "auto"
    max_tokens: int = 700
    ca_bundle: str = ""
    retries: int = 1

    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self.model)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["api_key"] = "<set>" if self.api_key else ""
        data["api_key_set"] = bool(self.api_key)
        return data


class CoreModelClient:
    """OpenAI-compatible model client used by Veyra Core cognition.

    The client reads state on every call so runtime config changes take effect
    without rebuilding the AwarenessLoop.
    """

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def config(self) -> CoreModelConfig:
        config = self.state_store.read_json("agent_config.json")
        core_model = config.get("core_model") if isinstance(config.get("core_model"), dict) else {}
        selected_name = str(config.get("selected_agent") or "openclaw")
        agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
        selected_agent = agents.get(selected_name) if isinstance(agents.get(selected_name), dict) else {}

        env_enabled = os.getenv("VEYRA_CORE_MODEL_ENABLED", "").lower() in {"1", "true", "yes", "on"}
        core_enabled = bool(core_model.get("enabled"))
        agent_enabled = bool(selected_agent.get("use_model_for_core"))
        enabled = bool(core_enabled or agent_enabled or env_enabled)

        api_key_env = str(
            _pick_config_value(
                core_model,
                selected_agent,
                core_key="api_key_env",
                agent_key="model_api_key_env",
                env_key="VEYRA_CORE_MODEL_API_KEY_ENV",
                default="VEYRA_CORE_MODEL_API_KEY",
                prefer_agent=agent_enabled and not core_enabled,
            )
        )
        api_key = str(
            _pick_config_value(
                core_model,
                selected_agent,
                core_key="api_key",
                agent_key="model_api_key",
                env_key=api_key_env,
                default=os.getenv("VEYRA_CORE_MODEL_API_KEY", ""),
                prefer_agent=agent_enabled and not core_enabled,
            )
        )
        base_url = str(
            _pick_config_value(
                core_model,
                selected_agent,
                core_key="base_url",
                agent_key="model_base_url",
                env_key="VEYRA_CORE_MODEL_BASE_URL",
                default="",
                prefer_agent=agent_enabled and not core_enabled,
            )
        ).rstrip("/")
        model = str(
            _pick_config_value(
                core_model,
                selected_agent,
                core_key="model",
                agent_key="model",
                env_key="VEYRA_CORE_MODEL",
                default="",
                prefer_agent=agent_enabled and not core_enabled,
            )
        )
        provider = str(
            _pick_config_value(
                core_model,
                selected_agent,
                core_key="provider",
                agent_key="model_provider",
                env_key="VEYRA_CORE_MODEL_PROVIDER",
                default="openai_compatible",
                prefer_agent=agent_enabled and not core_enabled,
            )
        )
        timeout = _float_or(
            _pick_config_value(
                core_model,
                selected_agent,
                core_key="timeout",
                agent_key="model_timeout",
                env_key="VEYRA_CORE_MODEL_TIMEOUT",
                default=20.0,
                prefer_agent=agent_enabled and not core_enabled,
            ),
            20.0,
        )
        max_tokens = int(
            _float_or(
                _pick_config_value(
                    core_model,
                    selected_agent,
                    core_key="max_tokens",
                    agent_key="model_max_tokens",
                    env_key="VEYRA_CORE_MODEL_MAX_TOKENS",
                    default=700,
                    prefer_agent=agent_enabled and not core_enabled,
                ),
                700,
            )
        )
        decision_mode = str(
            _pick_config_value(
                core_model,
                selected_agent,
                core_key="decision_mode",
                agent_key="model_decision_mode",
                env_key="",
                default="auto",
                prefer_agent=agent_enabled and not core_enabled,
            )
        )

        return CoreModelConfig(
            enabled=enabled,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            api_key_env=api_key_env,
            model=model,
            timeout=timeout,
            decision_mode=decision_mode if decision_mode in {"auto", "always"} else "auto",
            max_tokens=max(128, min(max_tokens, 2000)),
            ca_bundle=str(
                _pick_config_value(
                    core_model,
                    selected_agent,
                    core_key="ca_bundle",
                    agent_key="model_ca_bundle",
                    env_key="VEYRA_CORE_MODEL_CA_BUNDLE",
                    default="",
                    prefer_agent=agent_enabled and not core_enabled,
                )
            ),
            retries=max(
                0,
                min(
                    int(
                        _float_or(
                            _pick_config_value(
                                core_model,
                                selected_agent,
                                core_key="retries",
                                agent_key="model_retries",
                                env_key="VEYRA_CORE_MODEL_RETRIES",
                                default=1,
                                prefer_agent=agent_enabled and not core_enabled,
                            ),
                            1,
                        )
                    ),
                    3,
                ),
            ),
        )

    def status(self) -> dict[str, Any]:
        config = self.config()
        missing: list[str] = []
        if not config.enabled:
            missing.append("enabled")
        if not config.base_url:
            missing.append("base_url")
        if not config.model:
            missing.append("model")
        return {
            "enabled": config.enabled,
            "configured": config.configured(),
            "provider": config.provider,
            "base_url": config.base_url,
            "model": config.model,
            "api_key_env": config.api_key_env,
            "api_key_set": bool(config.api_key),
            "decision_mode": config.decision_mode,
            "max_tokens": config.max_tokens,
            "ca_bundle": _public_ca_bundle_path(self._ca_bundle_path(config)),
            "proxy": _proxy_summary(),
            "retries": config.retries,
            "status": "configured" if config.configured() else "unconfigured",
            "missing": missing,
        }

    def complete_json(self, *, system: str, user: str, purpose: str) -> dict[str, Any]:
        config = self.config()
        if not config.configured():
            return {"status": "unconfigured", "reason": "core model is not configured"}
        if config.provider != "openai_compatible":
            return {"status": "unsupported_provider", "provider": config.provider}

        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": config.max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        request = Request(self._chat_completions_url(config), data=data, headers=headers, method="POST")
        context, ca_bundle = self._ssl_context(config)
        last_error: Exception | None = None
        for attempt in range(config.retries + 1):
            try:
                with urlopen(request, timeout=config.timeout, context=context) as response:
                    body = response.read().decode("utf-8")
                break
            except HTTPError as exc:
                return {"status": "http_error", "purpose": purpose, "status_code": exc.code, "error": str(exc), "ca_bundle": _public_ca_bundle_path(ca_bundle)}
            except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
                last_error = exc
                if attempt >= config.retries:
                    return {
                        "status": "error",
                        "purpose": purpose,
                        "error": str(exc),
                        "ca_bundle": _public_ca_bundle_path(ca_bundle),
                        "proxy": _proxy_summary(),
                        "attempts": attempt + 1,
                    }
                time.sleep(min(0.25 * (attempt + 1), 1.0))
        else:
            return {"status": "error", "purpose": purpose, "error": str(last_error or "unknown model transport error")}

        try:
            raw = json.loads(body or "{}")
        except json.JSONDecodeError:
            return {"status": "invalid_response", "purpose": purpose, "raw_text": body[:1000]}

        content = self._content_from_response(raw)
        parsed = _parse_json_object(content)
        if parsed is None:
            return {"status": "invalid_json", "purpose": purpose, "raw_text": content[:1000]}
        model_status = parsed.get("status")
        parsed["status"] = "model_assisted"
        if model_status and model_status != "model_assisted":
            parsed["model_status"] = model_status
        parsed["_model"] = {
            "purpose": purpose,
            "provider": config.provider,
            "model": config.model,
            "ca_bundle": _public_ca_bundle_path(ca_bundle),
        }
        return parsed

    def _chat_completions_url(self, config: CoreModelConfig) -> str:
        if config.base_url.endswith("/chat/completions"):
            return config.base_url
        return f"{config.base_url}/chat/completions"

    def _ssl_context(self, config: CoreModelConfig) -> tuple[ssl.SSLContext, str]:
        ca_bundle = self._ca_bundle_path(config)
        if ca_bundle:
            return ssl.create_default_context(cafile=ca_bundle), ca_bundle
        return ssl.create_default_context(), ""

    def _ca_bundle_path(self, config: CoreModelConfig) -> str:
        candidates = [
            config.ca_bundle,
            os.getenv("REQUESTS_CA_BUNDLE", ""),
            os.getenv("SSL_CERT_FILE", ""),
            os.getenv("CURL_CA_BUNDLE", ""),
        ]
        try:
            import certifi

            candidates.append(certifi.where())
        except Exception:
            pass
        for candidate in candidates:
            path = str(candidate or "").strip()
            if path and Path(path).expanduser().exists():
                return str(Path(path).expanduser())
        return ""

    def _content_from_response(self, raw: dict[str, Any]) -> str:
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first.get("message"), dict) else {}
            content = message.get("content") or first.get("text") or ""
            return str(content)
        if isinstance(raw.get("content"), str):
            return str(raw["content"])
        return json.dumps(raw, ensure_ascii=False)


def redact_sensitive(value: Any, *, max_string: int = 1200, max_list: int | None = None) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SENSITIVE_KEY_MARKERS):
                redacted[key] = "<redacted>" if item else item
            else:
                redacted[key] = redact_sensitive(item, max_string=max_string, max_list=max_list)
        return redacted
    if isinstance(value, list):
        items = value if max_list is None else value[:max_list]
        return [redact_sensitive(item, max_string=max_string, max_list=max_list) for item in items]
    if isinstance(value, str):
        text = re.sub(r"/Users/[^\s,;:)]+", "<local_path>", value)
        text = re.sub(r"(?i)(api[_-]?key|token|secret|password)=([A-Za-z0-9._~+/=-]+)", r"\1=<redacted>", text)
        return text[:max_string]
    return value


def _public_ca_bundle_path(path: str) -> str:
    if not path:
        return ""
    return "<certifi>" if "certifi" in path.lower() else redact_sensitive(path)


def _proxy_summary() -> dict[str, bool]:
    return {
        key: bool(os.getenv(key) or os.getenv(key.lower()))
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    }


def _parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _float_or(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pick_config_value(
    core_model: dict[str, Any],
    selected_agent: dict[str, Any],
    *,
    core_key: str,
    agent_key: str,
    env_key: str,
    default: Any,
    prefer_agent: bool,
) -> Any:
    if prefer_agent and selected_agent.get(agent_key) not in {None, ""}:
        return selected_agent.get(agent_key)
    if core_model.get(core_key) not in {None, ""}:
        return core_model.get(core_key)
    if selected_agent.get(agent_key) not in {None, ""}:
        return selected_agent.get(agent_key)
    if env_key:
        return os.getenv(env_key, default)
    return default
