from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.definitions import RiskLevel
from core.state_compact import compact_persona_binding
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, utc_now_iso


class PersonaEngine:
    def __init__(self, state_store: WorldStateStore | None = None) -> None:
        self.state_store = state_store
        self.persona_dir = Path(__file__).resolve().parents[1] / "personas"
        self._catalog_cache: dict[str, dict[str, Any]] | None = None

    def patch_for(
        self,
        text: str,
        risk_level: RiskLevel,
        *,
        channel: str = "api",
        route: str | None = None,
        target_agent: str | None = None,
        decision: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        modes = ["Minimalist"]
        lowered = text.lower()
        if any(marker in lowered for marker in ["端口", "进程", "部署", "服务", "openclaw", "hermes", "飞书", "feishu", "discord"]):
            modes.append("Operator")
        if any(marker in lowered for marker in ["代码", "实现", "修复", "重构", "开发"]):
            modes.append("Engineer")
        if any(marker in lowered for marker in ["解释", "为什么", "教学", "说明", "teach"]):
            modes.append("Teacher")
        if any(marker in lowered for marker in ["计划", "拆解", "架构", "路线", "多步骤"]):
            modes.append("Planner")
        if channel in {"feishu", "discord", "slack"}:
            modes.append("Steward")
        if risk_level in {RiskLevel.R3, RiskLevel.R4, RiskLevel.R5}:
            modes.append("Guardian")
        if route == "agent" or target_agent:
            modes.append("Operator")
        modes = list(dict.fromkeys(modes))
        token_budget = self._token_budget(channel=channel, risk_level=risk_level, route=route)
        loaded = self._loaded_personas(modes)
        response_style = self._response_style(channel, loaded)
        tool_policy = self._tool_policy(risk_level, loaded)
        guidelines = self._guidelines(loaded)
        context_budget_chars = self._context_budget_chars(token_budget, loaded)
        return {
            "mode": modes,
            "channel": channel,
            "route": route,
            "target_agent": target_agent,
            "risk_level": risk_level.value,
            "response_style": response_style,
            "token_budget": token_budget,
            "context_budget_chars": context_budget_chars,
            "tool_policy": tool_policy,
            "guidelines": guidelines,
            "loaded_personas": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "source": item.get("source"),
                    "risk_bias": item.get("risk_bias"),
                    "tool_bias": item.get("tool_bias"),
                }
                for item in loaded
            ],
            "agent_policy": {
                "selected_agent": target_agent,
                "dispatch": "allowed_with_evidence" if risk_level in {RiskLevel.R0, RiskLevel.R1, RiskLevel.R2} else "review_required",
                "requires_tool_proxy_evidence": True,
            },
            "memory_policy": {
                "read": "focus_ranked",
                "write": "filtered_after_verification",
                "channel_context": channel,
            },
            "review_policy": {
                "r3_r4": "ask_user",
                "r5": "block",
            },
            "behavior": [
                "prefer read-only diagnosis first",
                "ask before destructive or hard-to-revert actions",
                "return evidence for execution claims",
                f"format for {channel} channel using {response_style}",
            ] + guidelines[:4],
            "decision_binding": decision or {},
        }

    def record_binding(self, event: VeyraEvent, persona_patch: dict[str, Any]) -> dict[str, Any]:
        if not self.state_store:
            return {"status": "skipped", "reason": "no state_store"}
        binding = compact_persona_binding(
            {
                "event_id": event.event_id,
                "channel": event.source.channel,
                "session_id": event.source.session_id,
                "active_modes": persona_patch.get("mode", []),
                "route": persona_patch.get("route"),
                "target_agent": persona_patch.get("target_agent"),
                "risk_level": persona_patch.get("risk_level"),
                "response_style": persona_patch.get("response_style"),
                "context_budget_chars": persona_patch.get("context_budget_chars"),
                "guideline_count": len(persona_patch.get("guidelines", [])) if isinstance(persona_patch.get("guidelines"), list) else 0,
                "updated_at": utc_now_iso(),
            }
        )

        def update_persona(state: dict[str, Any]) -> dict[str, Any]:
            history = state.get("history") if isinstance(state.get("history"), list) else []
            history.append(binding)
            state["history"] = history[-50:]
            state["active_modes"] = persona_patch.get("mode", [])
            state["last_binding"] = binding
            state["updated_at"] = utc_now_iso()
            return state

        self.state_store.mutate_json("persona_state.json", update_persona)
        return {"status": "success", "binding": binding}

    def _response_style(self, channel: str, loaded: list[dict[str, Any]]) -> str:
        if channel in {"feishu", "discord", "slack"}:
            return "compact_chat_with_clear_next_action"
        if channel == "console":
            return "operator_console_structured"
        ids = {str(item.get("id") or "").lower() for item in loaded}
        if "minimalist" in ids:
            return "direct_compact"
        if "teacher" in ids:
            return "direct_explanatory"
        if "operator" in ids:
            return "evidence_first_operational"
        return "direct_structured"

    def _token_budget(self, *, channel: str, risk_level: RiskLevel, route: str | None) -> dict[str, int]:
        base = 500 if channel in {"feishu", "discord", "slack"} else 900
        if route == "agent":
            base += 400
        if risk_level in {RiskLevel.R3, RiskLevel.R4, RiskLevel.R5}:
            base += 250
        return {"response_tokens": base, "agent_context_tokens": max(base * 2, 1200)}

    def _tool_policy(self, risk_level: RiskLevel, loaded: list[dict[str, Any]]) -> dict[str, str]:
        tool_biases = {str(item.get("tool_bias") or "") for item in loaded}
        if risk_level == RiskLevel.R5:
            return {"default": "block", "rationale": "forbidden risk"}
        if risk_level in {RiskLevel.R3, RiskLevel.R4}:
            return {"default": "review_required", "rationale": "medium/high impact"}
        if "avoid_unnecessary_tools" in tool_biases:
            return {"default": "direct_when_evidence_sufficient", "rationale": "minimalist persona avoids unnecessary tools"}
        if "code_and_tests" in tool_biases:
            return {"default": "code_grounded_with_tests", "rationale": "engineer persona requires implementation evidence"}
        if "read_only_probe_first" in tool_biases:
            return {"default": "read_only_probe_first", "rationale": "operator persona requires fresh runtime evidence"}
        if "review_before_execute" in tool_biases:
            return {"default": "review_required", "rationale": "guardian persona is active"}
        return {"default": "read_only_first", "rationale": "bounded low-risk handling"}

    def _context_budget_chars(self, token_budget: dict[str, int], loaded: list[dict[str, Any]]) -> int:
        base = max(int(token_budget.get("agent_context_tokens") or 1200) * 4, 1800)
        ids = {str(item.get("id") or "").lower() for item in loaded}
        if "minimalist" in ids:
            base = min(base, 3600)
        if "engineer" in ids or "operator" in ids:
            base = max(base, 6000)
        if "guardian" in ids:
            base = max(base, 6800)
        return max(1800, min(base, 9000))

    def _loaded_personas(self, modes: list[str]) -> list[dict[str, Any]]:
        catalog = self._catalog()
        output: list[dict[str, Any]] = []
        for mode in modes:
            key = self._persona_key(mode)
            if key in catalog:
                output.append(catalog[key])
        return output

    def _guidelines(self, loaded: list[dict[str, Any]]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for persona in loaded:
            for guideline in persona.get("guidelines", []) if isinstance(persona.get("guidelines"), list) else []:
                text = str(guideline).strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                output.append(text)
        return output[:12]

    def _catalog(self) -> dict[str, dict[str, Any]]:
        if self._catalog_cache is not None:
            return self._catalog_cache
        catalog: dict[str, dict[str, Any]] = {}
        registry = self._read_registry()
        persona_ids = set(registry)
        if self.persona_dir.exists():
            for path in self.persona_dir.glob("*.persona.*"):
                persona_ids.add(path.name.split(".persona.", 1)[0])
        for persona_id in sorted(persona_ids):
            catalog[persona_id] = self._load_persona(persona_id)
        self._catalog_cache = catalog
        return catalog

    def _read_registry(self) -> list[str]:
        path = self.persona_dir / "registry.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return []
        personas = data.get("personas") if isinstance(data, dict) else []
        return [self._persona_key(str(item)) for item in personas if str(item).strip()]

    def _load_persona(self, persona_id: str) -> dict[str, Any]:
        json_payload: dict[str, Any] = {}
        json_path = self.persona_dir / f"{persona_id}.persona.json"
        if json_path.exists():
            try:
                raw = json.loads(json_path.read_text(encoding="utf-8") or "{}")
                if isinstance(raw, dict):
                    json_payload = raw
            except (OSError, json.JSONDecodeError):
                json_payload = {"load_error": "invalid_json"}
        md_path = self.persona_dir / f"{persona_id}.persona.md"
        guidelines = self._read_guidelines(md_path)
        name = str(json_payload.get("name") or self._title_name(persona_id))
        return {
            "id": persona_id,
            "name": name,
            "source": str(md_path if md_path.exists() else json_path),
            "risk_bias": json_payload.get("risk_bias"),
            "tool_bias": json_payload.get("tool_bias"),
            "guidelines": guidelines,
        }

    def _read_guidelines(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        guidelines: list[str] = []
        for raw in lines:
            line = raw.strip()
            if not line.startswith("- "):
                continue
            text = line[2:].strip()
            if text:
                guidelines.append(text)
        return guidelines[:8]

    def _persona_key(self, value: str) -> str:
        return value.strip().lower().replace(" ", "_")

    def _title_name(self, persona_id: str) -> str:
        return " ".join(part.capitalize() for part in persona_id.split("_"))
