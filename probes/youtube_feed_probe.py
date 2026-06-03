from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from probes.http_utils import fetch_text
from probes.schema import probe_payload

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}

def _known_channel_ids() -> dict[str, str]:
    import json
    import os

    raw = os.getenv("VEYRA_YOUTUBE_CHANNEL_MAP", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v} if isinstance(data, dict) else {}


class YoutubeFeedProbe:
    """Fetch the latest video title from a YouTube channel RSS feed when channel_id is known."""

    def run(self, *, creator: str, channel_id: str | None = None) -> dict[str, Any]:
        creator = (creator or "").strip()
        channel_id = (channel_id or _known_channel_ids().get(creator) or "").strip()
        if not channel_id:
            return probe_payload(
                probe="youtube_feed_probe",
                target=creator or "youtube",
                status="missing_target",
                summary=f"No known YouTube channel id for {creator}.",
                confidence=0.3,
                ttl_seconds=600,
                details={"creator": creator},
            )
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        try:
            body = fetch_text(feed_url, timeout=12.0, headers={"User-Agent": "Veyra-YoutubeFeedProbe/0.1"})
        except Exception as exc:
            return probe_payload(
                probe="youtube_feed_probe",
                target=creator,
                status="unavailable",
                summary=f"YouTube RSS unavailable for {creator}: {exc}",
                confidence=0.35,
                ttl_seconds=300,
                details={"creator": creator, "channel_id": channel_id, "feed_url": feed_url, "error": str(exc)},
            )
        entry = self._latest_entry(body)
        if not entry:
            return probe_payload(
                probe="youtube_feed_probe",
                target=creator,
                status="empty",
                summary=f"YouTube RSS returned no entries for {creator}.",
                confidence=0.4,
                ttl_seconds=600,
                details={"creator": creator, "channel_id": channel_id, "feed_url": feed_url},
            )
        title = str(entry.get("title") or "").strip()
        published = str(entry.get("published") or "").strip()
        link = str(entry.get("link") or "").strip()
        summary = f"{creator} 最新视频标题：{title}"
        return probe_payload(
            probe="youtube_feed_probe",
            target=creator,
            status="ok",
            summary=summary,
            confidence=0.9,
            ttl_seconds=1800,
            details={
                "creator": creator,
                "channel_id": channel_id,
                "feed_url": feed_url,
                "title": title,
                "published_at": published,
                "url": link,
                "source": "official_youtube_rss",
            },
            claims=[
                {
                    "key": f"youtube_latest:{creator}",
                    "claim": summary,
                    "confidence": 0.9,
                    "source": "official_youtube_rss",
                    "ttl_seconds": 1800,
                }
            ],
        )

    def _latest_entry(self, body: str) -> dict[str, str] | None:
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return None
        for entry in root.findall("atom:entry", ATOM_NS):
            title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
            published = (entry.findtext("atom:published", default="", namespaces=ATOM_NS) or "").strip()
            link_el = entry.find("atom:link", ATOM_NS)
            link = ""
            if link_el is not None:
                link = str(link_el.attrib.get("href") or "").strip()
            if title:
                return {"title": title, "published": published, "link": link}
        return None


def search_query_for_creator(creator: str) -> str:
    return f"{creator} site:youtube.com latest video"
