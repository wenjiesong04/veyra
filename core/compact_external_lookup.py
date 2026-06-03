from __future__ import annotations

from typing import Any

from core.execution_tier import compact_lookup_target
from probes.search_probe import SearchProbe
from probes.youtube_feed_probe import YoutubeFeedProbe, search_query_for_creator


class CompactExternalLookup:
    """L1 path: small structured task, no heavy AgentTaskPacket, no long-term memory."""

    def __init__(self) -> None:
        self.youtube = YoutubeFeedProbe()
        self.search = SearchProbe()

    def run(self, text: str) -> dict[str, Any]:
        target = compact_lookup_target(text)
        if not target:
            return {"status": "unsupported", "reason": "not_a_compact_lookup"}
        creator = str((target.get("target") or {}).get("creator") or "")
        rss = self.youtube.run(creator=creator)
        if str(rss.get("status")) == "ok":
            return self._success_from_rss(target, rss)
        search = self.search.run(search_query_for_creator(creator), max_results=5)
        if str(search.get("status")) == "ok":
            return self._success_from_search(target, search, creator)
        return {
            "status": "failed",
            "reason": "compact_lookup_no_evidence",
            "compact_task": target,
            "rss": rss,
            "search": search,
        }

    def _success_from_rss(self, target: dict[str, Any], rss: dict[str, Any]) -> dict[str, Any]:
        details = rss.get("details") if isinstance(rss.get("details"), dict) else {}
        title = str(details.get("title") or "").strip()
        published = str(details.get("published_at") or "").strip()
        url = str(details.get("url") or "").strip()
        creator = str(details.get("creator") or (target.get("target") or {}).get("creator") or "")
        response = self._format_response(creator=creator, title=title, source="official_youtube_rss", published=published, url=url)
        return {
            "status": "ok",
            "compact_task": target,
            "provider": "youtube_feed_probe",
            "probe_result": rss,
            "response": response,
        }

    def _success_from_search(self, target: dict[str, Any], search: dict[str, Any], creator: str) -> dict[str, Any]:
        details = search.get("details") if isinstance(search.get("details"), dict) else {}
        results = details.get("results") if isinstance(details.get("results"), list) else []
        top = results[0] if results and isinstance(results[0], dict) else {}
        title = str(top.get("title") or "").strip()
        url = str(top.get("url") or "").strip()
        source = str(top.get("source") or "web_search")
        response = self._format_response(creator=creator, title=title, source=source, published="", url=url)
        return {
            "status": "ok",
            "compact_task": target,
            "provider": "search_probe",
            "probe_result": search,
            "response": response,
        }

    def _format_response(self, *, creator: str, title: str, source: str, published: str, url: str) -> str:
        lines = [f"{creator} 在 YouTube 的最新视频标题是：{title or '（未能解析标题）'}"]
        lines.append(f"来源：{source or 'unknown'}")
        if published:
            lines.append(f"发布时间：{published}")
        if url:
            lines.append(f"链接：{url}")
        return "\n".join(lines)
