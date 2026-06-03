from __future__ import annotations

import json
import re
from typing import Any

from interface.agent_adapter import ExecutionResult


class ResultInterpreter:
    """Turn raw Agent/tool execution output into evidence-oriented observations."""

    def interpret_execution(self, execution: ExecutionResult, verification: dict[str, Any]) -> dict[str, Any]:
        raw = execution.raw if isinstance(execution.raw, dict) else {}
        result_text = execution.result.strip()
        extracted = self._extract_structured_result(raw)
        summary = extracted or self._compact_text(result_text)
        return {
            "status": execution.status,
            "executor": execution.executor,
            "summary": summary,
            "evidence": {
                "task_id": execution.task_id,
                "changed_files": execution.changed_files,
                "tool_calls": execution.tool_calls,
                "verification_status": verification.get("status"),
                "verification_verdict": verification.get("verdict"),
                "raw_keys": sorted(raw.keys()),
            },
            "failure_reason": self._failure_reason(execution, verification),
            "next_action": verification.get("next_action"),
            "raw_result_type": self._raw_result_type(raw, result_text),
        }

    def _extract_structured_result(self, raw: dict[str, Any]) -> str:
        candidates = []
        for key in ("answer", "summary", "title", "result"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        results = raw.get("results")
        if isinstance(results, list) and results:
            titles = []
            for item in results[:5]:
                if isinstance(item, dict):
                    title = item.get("title") or item.get("name") or item.get("headline")
                    if title:
                        titles.append(str(title))
                elif isinstance(item, str):
                    titles.append(item)
            if titles:
                candidates.append("; ".join(titles))
        return self._compact_text(candidates[0]) if candidates else ""

    def _failure_reason(self, execution: ExecutionResult, verification: dict[str, Any]) -> str:
        if execution.status in {"success", "submitted", "running", "pending"}:
            return ""
        return str(verification.get("verdict") or verification.get("message") or execution.result or execution.status)

    def _raw_result_type(self, raw: dict[str, Any], result_text: str) -> str:
        if "results" in raw:
            return "search_results"
        if "content" in raw:
            return "fetched_content"
        if result_text.startswith("{") or result_text.startswith("["):
            return "json_text"
        return "text"

    def _compact_text(self, value: str, limit: int = 900) -> str:
        text = " ".join(str(value or "").split())
        text = self._strip_internal_preamble(text)
        if not text:
            return ""
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _strip_internal_preamble(self, text: str) -> str:
        if not text:
            return ""
        preamble_markers = ("用户", "用户说", "我需要", "我应该", "让我直接", "之前的搜索结果", "重新搜索", "系统可能")
        answer_markers = (
            "抱歉之前的混淆，",
            "抱歉之前的混淆。",
            "根据我之前的搜索结果：",
            "根据我之前的搜索结果，",
            "根据之前的搜索结果：",
            "根据搜索结果：",
            "最终答案：",
            "答案是：",
            "答案：",
            "结论：",
        )
        if not any(marker in text[:520] for marker in preamble_markers):
            return text
        title_matches = re.findall(r"标题(?:是|为)?[：:]?\s*(?:(?:>)|(?:\*)|(?:-)|\s)*((?:\*\*)?《[^》]{2,160}》(?:\*\*)?)", text)
        if title_matches:
            return f"标题是：{title_matches[-1].strip()}"
        for marker in answer_markers:
            index = text.rfind(marker)
            if index > 0:
                return text[index + len(marker) :].strip()
        match = re.search(r"((?:\*\*)?[^。]{0,80}(?:标题|答案)(?:是|为|：)[^\n]{8,})", text)
        if match and match.start() > 0:
            return match.group(1).strip()
        return text


def compact_json(value: Any, *, limit: int = 900) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
