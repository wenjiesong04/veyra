#!/usr/bin/env python3
"""S0 reflection sandbox: does a model-driven "what is new" pass produce value?

This script is deliberately outside the Veyra runtime:

- it never imports ``main``; it never constructs ``WorldStateStore``;
- it only reads state files, so it cannot take a writer lease or emit traces;
- it writes nothing under ``state/``; the previous answer is kept in a private
  sandbox directory that the runtime does not read;
- it never notifies anyone. Output goes to the terminal.

Its only purpose is the S0 decision in
``docs/veyra_cognitive_awakening_development.md``: look at the printed items and
judge whether a reflection loop is worth building. It is not a gate and it does
not assert that the model output is correct.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.env_loader import load_runtime_env  # noqa: E402

STATE_ROOT = Path(os.getenv("VEYRA_STATE_ROOT") or (ROOT / "state"))
SANDBOX_DIR = ROOT / ".reflection_sandbox"
LAST_ANSWER_FILE = SANDBOX_DIR / "last_answer.json"

MAX_FRESH_CLAIMS = 40
MAX_STALE_CLAIM_KEYS = 30
MAX_EXTERNAL_SUMMARIES = 5
MAX_ITEMS = 5

SYSTEM_PROMPT = """你是 Veyra 的内省环节。你会定期查看系统对世界的当前认知快照，回答一个问题：

如果用户现在问你「有什么新情况」，你会说什么？

严格规则：

1. 你只能陈述快照里有证据支持的事。每一条都必须给出 evidence_refs，格式只能是：
   - "probe:<probe_name>"    指向 probes 里的一项
   - "belief:<claim_key>"    指向 beliefs 里的一项
   - "external:<item_id>"    指向 external 里的一项
   引用不存在的 id 会被系统丢弃，等于这条没说。

2. 多数时候世界没有值得打扰用户的变化。这时返回 {"nothing_new": true, "items": []} 是正确且被鼓励的行为。
   不要为了显得有用而编造、凑数或把陈旧信息重新包装成新情况。

3. 如果给了上一次的回答，只报告相对上次的**变化**：新出现的、反转的、升级的、已解决的、以及本来可信但现在已经过期的。
   上次说过且没有变化的事，不要重复。

4. topic 必须是稳定的短标识（如 "disk_pressure"、"openclaw_gateway"），同一件事在不同轮次必须用同一个 topic。

5. 判断"用户会不会想知道"的标准是：它是否影响用户当前能做的事、是否需要用户做决定、是否是坏消息的早期信号。
   系统内部的正常波动不值得报告。

只输出 JSON，不要解释。schema：

{
  "nothing_new": false,
  "headline": "一句话概括最值得知道的事",
  "items": [
    {
      "topic": "稳定短标识",
      "statement": "具体陈述",
      "change_kind": "new|reversed|escalated|resolved|stale_warning",
      "evidence_refs": ["probe:system_probe"],
      "why_user_cares": "为什么用户会想知道",
      "confidence": 0.8
    }
  ],
  "questions_i_cannot_answer": ["当前证据不足以判断的事"]
}"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def find_state_file(name: str) -> Path | None:
    direct = STATE_ROOT / name
    if direct.exists():
        return direct
    for candidate in sorted(STATE_ROOT.glob(f"*/{name}")):
        return candidate
    return None


def load_snapshot() -> dict[str, Any]:
    """Read the parts of world state that actually carry meaning.

    The event/situation pipeline is deliberately skipped: its payloads are
    redacted to length metadata, so it carries relations without content.
    """

    probes: dict[str, Any] = {}
    local_world_path = find_state_file("local_world.json")
    if local_world_path:
        raw_probes = read_json(local_world_path).get("probes")
        if isinstance(raw_probes, dict):
            for name, observation in raw_probes.items():
                if not isinstance(observation, dict):
                    continue
                probes[str(name)] = {
                    "status": observation.get("status"),
                    "summary": observation.get("summary"),
                    "observed_at": observation.get("observed_at"),
                    "confidence": observation.get("confidence"),
                    "ttl_seconds": observation.get("ttl_seconds"),
                }

    fresh_claims: list[dict[str, Any]] = []
    stale_keys: list[str] = []
    belief_path = find_state_file("belief_state.json")
    if belief_path:
        raw_claims = read_json(belief_path).get("claims")
        if isinstance(raw_claims, list):
            ordered = sorted(
                (item for item in raw_claims if isinstance(item, dict)),
                key=lambda item: str(item.get("updated_at") or ""),
                reverse=True,
            )
            # Historic state can hold many rows per key; keep only the newest
            # per key so one repeated probe cannot flood the snapshot.
            seen_keys: set[str] = set()
            for claim in ordered:
                key = str(claim.get("key") or "")
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                status = str(claim.get("status") or "")
                if status in {"fresh", "conflict"} and len(fresh_claims) < MAX_FRESH_CLAIMS:
                    fresh_claims.append(
                        {
                            "key": key,
                            "claim": claim.get("claim"),
                            "status": status,
                            "confidence": claim.get("confidence"),
                            "source": claim.get("source"),
                            "updated_at": claim.get("updated_at"),
                        }
                    )
                elif status == "stale" and len(stale_keys) < MAX_STALE_CLAIM_KEYS:
                    stale_keys.append(key)

    external: list[dict[str, Any]] = []
    external_path = find_state_file("external_world.json")
    if external_path:
        external_state = read_json(external_path)
        summaries = external_state.get("summaries")
        if isinstance(summaries, list):
            seen_ids: set[str] = set()
            for index, summary in enumerate(reversed(summaries)):
                if not isinstance(summary, dict) or len(external) >= MAX_EXTERNAL_SUMMARIES:
                    continue
                item_id = str(summary.get("watchlist_id") or f"summary_{index}")
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                external.append(
                    {
                        "id": item_id,
                        "topic": summary.get("topic"),
                        "summary": summary.get("summary"),
                        "refreshed_at": summary.get("refreshed_at") or summary.get("updated_at"),
                    }
                )

    return {
        "captured_at": utc_now(),
        "probes": probes,
        "beliefs": fresh_claims,
        "stale_belief_keys": stale_keys,
        "external": external,
    }


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    """Digest of the meaningful content only, so timestamps do not force calls."""

    material = {
        "probes": {
            name: {"status": data.get("status"), "summary": data.get("summary")}
            for name, data in sorted(snapshot.get("probes", {}).items())
        },
        "beliefs": sorted(
            (str(item.get("key")), str(item.get("claim")), str(item.get("status")))
            for item in snapshot.get("beliefs", [])
        ),
        "stale": sorted(snapshot.get("stale_belief_keys", [])),
        "external": sorted(
            (str(item.get("id")), str(item.get("summary"))) for item in snapshot.get("external", [])
        ),
    }
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def known_evidence_ids(snapshot: dict[str, Any]) -> set[str]:
    known = {f"probe:{name}" for name in snapshot.get("probes", {})}
    known |= {f"belief:{item.get('key')}" for item in snapshot.get("beliefs", []) if item.get("key")}
    known |= {f"belief:{key}" for key in snapshot.get("stale_belief_keys", []) if key}
    known |= {f"external:{item.get('id')}" for item in snapshot.get("external", []) if item.get("id")}
    return known


def build_user_prompt(snapshot: dict[str, Any], last_answer: dict[str, Any] | None) -> str:
    sections = [
        "## 当前世界快照",
        json.dumps(
            {
                "probes": snapshot.get("probes", {}),
                "beliefs": snapshot.get("beliefs", []),
                "stale_belief_keys": snapshot.get("stale_belief_keys", []),
                "external": snapshot.get("external", []),
            },
            ensure_ascii=False,
            indent=2,
        ),
    ]
    if last_answer:
        sections += [
            "",
            f"## 上一次回答（{last_answer.get('answered_at', 'unknown')}）",
            json.dumps(last_answer.get("answer", {}), ensure_ascii=False, indent=2),
            "",
            "只报告相对上一次的变化。没有变化就返回 nothing_new。",
        ]
    else:
        sections += ["", "## 这是第一次内省", "没有上一次回答可比较，请报告当前最值得用户知道的事。"]
    return "\n".join(sections)


def model_config() -> dict[str, Any]:
    config_path = find_state_file("agent_config.json")
    core_model = read_json(config_path).get("core_model") if config_path else {}
    core_model = core_model if isinstance(core_model, dict) else {}
    api_key_env = str(core_model.get("api_key_env") or "VEYRA_CORE_MODEL_API_KEY")
    return {
        "base_url": str(core_model.get("base_url") or "").rstrip("/"),
        "model": str(core_model.get("model") or ""),
        "api_key": os.getenv(api_key_env, ""),
        "api_key_env": api_key_env,
        "timeout": float(core_model.get("timeout") or 30.0),
        "max_tokens": int(core_model.get("max_tokens") or 1600),
    }


def call_model(system: str, user: str, config: dict[str, Any]) -> dict[str, Any]:
    if not config["base_url"] or not config["model"]:
        return {"status": "unconfigured", "reason": "core_model.base_url/model missing"}
    if not config["api_key"]:
        return {"status": "auth_missing", "reason": f"{config['api_key_env']} not set"}

    payload = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": config["max_tokens"],
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    request = Request(
        f"{config['base_url']}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['api_key']}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=config["timeout"], context=ssl.create_default_context()) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        return {"status": "http_error", "status_code": exc.code, "error": str(exc)}
    except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        return {"status": "transport_error", "error": str(exc)}

    try:
        raw = json.loads(body or "{}")
        content = raw["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        return {"status": "invalid_json", "error": str(exc), "raw": body[:500]}
    if not isinstance(parsed, dict):
        return {"status": "invalid_json", "error": "top level is not an object"}
    parsed["status"] = "ok"
    return parsed


def validate_items(answer: dict[str, Any], snapshot: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Drop any item whose evidence cannot be resolved in the snapshot."""

    known = known_evidence_ids(snapshot)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    raw_items = answer.get("items")
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict):
            continue
        refs = item.get("evidence_refs")
        refs = [str(ref) for ref in refs] if isinstance(refs, list) else []
        unknown = [ref for ref in refs if ref not in known]
        if not refs or unknown:
            rejected.append({"item": item, "reason": "unresolved_evidence", "unknown_refs": unknown or ["<none>"]})
            continue
        accepted.append(item)
    return accepted[:MAX_ITEMS], rejected


def load_last_answer() -> dict[str, Any] | None:
    if not LAST_ANSWER_FILE.exists():
        return None
    data = read_json(LAST_ANSWER_FILE)
    return data or None


def save_last_answer(answer: dict[str, Any], digest: str) -> None:
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    LAST_ANSWER_FILE.write_text(
        json.dumps(
            {"answered_at": utc_now(), "snapshot_digest": digest, "answer": answer},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def print_pass(snapshot: dict[str, Any], digest: str, result: dict[str, Any]) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    probes = snapshot.get("probes", {})
    print(f"\n{'=' * 72}")
    print(f"[{stamp}] snapshot digest {digest[:12]}  probes={len(probes)}  "
          f"fresh_beliefs={len(snapshot.get('beliefs', []))}  "
          f"stale_keys={len(snapshot.get('stale_belief_keys', []))}  "
          f"external={len(snapshot.get('external', []))}")

    status = result.get("status")
    if status == "skipped_no_change":
        print("  world unchanged since last pass -> no model call (cost saved)")
        return
    if status != "ok":
        print(f"  MODEL UNAVAILABLE: {status} {result.get('error') or result.get('reason') or ''}")
        return

    accepted = result.get("_accepted", [])
    rejected = result.get("_rejected", [])

    if result.get("nothing_new") and not accepted:
        print("  nothing_new -> the model reports no change worth interrupting for")
    else:
        headline = result.get("headline")
        if headline:
            print(f"  HEADLINE: {headline}")
        for index, item in enumerate(accepted, 1):
            print(f"\n  [{index}] {item.get('topic')}  ({item.get('change_kind')}, conf={item.get('confidence')})")
            print(f"      {item.get('statement')}")
            print(f"      why: {item.get('why_user_cares')}")
            print(f"      evidence: {', '.join(str(ref) for ref in item.get('evidence_refs', []))}")

    if rejected:
        print(f"\n  REJECTED {len(rejected)} item(s) with unresolvable evidence:")
        for entry in rejected:
            item = entry.get("item", {})
            print(f"      - {item.get('topic')}: {item.get('statement')}")
            print(f"        unknown refs: {', '.join(entry.get('unknown_refs', []))}")

    unknowns = result.get("questions_i_cannot_answer")
    if isinstance(unknowns, list) and unknowns:
        print("\n  cannot answer yet:")
        for question in unknowns[:3]:
            print(f"      - {question}")


def run_once(*, save: bool = True, force: bool = False) -> dict[str, Any]:
    snapshot = load_snapshot()
    digest = snapshot_digest(snapshot)
    last = load_last_answer()

    if not force and last and last.get("snapshot_digest") == digest:
        result = {"status": "skipped_no_change"}
        print_pass(snapshot, digest, result)
        return result

    result = call_model(SYSTEM_PROMPT, build_user_prompt(snapshot, last), model_config())
    if result.get("status") == "ok":
        accepted, rejected = validate_items(result, snapshot)
        result["_accepted"] = accepted
        result["_rejected"] = rejected
        if save:
            save_last_answer(
                {
                    "nothing_new": bool(result.get("nothing_new")) and not accepted,
                    "headline": result.get("headline"),
                    "items": accepted,
                },
                digest,
            )
    print_pass(snapshot, digest, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="S0 reflection sandbox (read-only, prints only)")
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--interval", type=int, default=600, help="seconds between passes in loop mode")
    parser.add_argument("--force", action="store_true", help="call the model even if the world did not change")
    parser.add_argument("--no-save", action="store_true", help="do not remember this answer for the next pass")
    parser.add_argument("--show-snapshot", action="store_true", help="print the raw snapshot and exit")
    args = parser.parse_args()

    load_runtime_env()

    if args.show_snapshot:
        snapshot = load_snapshot()
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        print(f"\ndigest: {snapshot_digest(snapshot)}")
        print(f"evidence ids: {len(known_evidence_ids(snapshot))}")
        return 0

    print(f"reflection sandbox | state={STATE_ROOT} | read-only | writes nothing under state/")
    if not args.loop:
        result = run_once(save=not args.no_save, force=args.force)
        return 0 if result.get("status") in {"ok", "skipped_no_change"} else 1

    print(f"loop mode, every {args.interval}s. Ctrl-C to stop.")
    try:
        while True:
            run_once(save=not args.no_save, force=args.force)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
