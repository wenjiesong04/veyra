#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.model_client as model_client_module  # noqa: E402
from core.model_client import CoreModelClient  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.channel_adapter import ChannelAdapter  # noqa: E402
from probes.search_probe import SearchProbe  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> int:
    with TemporaryDirectory(prefix="veyra-provider-config-") as tmp:
        root = Path(tmp)
        previous_home = os.environ.get("HOME")
        previous_key = os.environ.pop("MOONSHOT_API_KEY", None)
        os.environ["HOME"] = str(root / "home")
        openclaw_env = Path(os.environ["HOME"]) / ".openclaw" / ".env"
        openclaw_env.parent.mkdir(parents=True, exist_ok=True)
        openclaw_env.write_text("MOONSHOT_API_KEY=local-openclaw-test-key\n", encoding="utf-8")
        model_client_module._LOCAL_ENV_CACHE = None
        try:
            store = WorldStateStore(root / "state")
            store.write_json(
                "agent_config.json",
                {
                    "selected_agent": "openclaw",
                    "agents": {"openclaw": {"kind": "openclaw", "base_url": "http://localhost:18789", "enabled": True}},
                    "core_model": {
                        "enabled": True,
                        "provider": "openai_compatible",
                        "base_url": "https://api.moonshot.cn/v1",
                        "api_key_env": "MOONSHOT_API_KEY",
                        "model": "moonshot-v1-auto",
                    },
                },
            )
            status = CoreModelClient(store).status()
            expect(status.get("configured"), "core model remains configured", status)
            expect(status.get("api_key_set"), "core model reads local OpenClaw env fallback", status)

            adapter = ChannelAdapter(store, channel="feishu")
            target = adapter._feishu_target(
                session_id="feishu:ou_example:oc_example_chat",
                metadata={"route": "commitment_push"},
                config={"reply_to_session": True, "default_receive_id": ""},
            )
            expect(target.get("receive_id") == "oc_example_chat", "Feishu target falls back to session chat_id", target)
            expect(target.get("receive_id_source") is None, "Feishu target keeps compact shape", target)
            expect(target.get("source") == "session_id", "Feishu target source is explicit", target)

            def fake_openclaw_search(command: list[str], timeout: float) -> str:
                expect(command[:4] == ["openclaw", "infer", "web", "search"], "OpenClaw search command is used", command)
                expect(timeout > 0, "OpenClaw search timeout is positive", timeout)
                return (
                    '{"results":[{"title":"PyTorch release notes",'
                    '"url":"https://pytorch.org/blog/release-notes/",'
                    '"snippet":"Latest important PyTorch updates."}]}'
                )

            search = SearchProbe(cli_runner=fake_openclaw_search)
            search_result = search.run("PyTorch latest release", max_results=3)
            results = search_result.get("details", {}).get("results") if isinstance(search_result.get("details"), dict) else []
            expect(search_result.get("status") == "ok", "OpenClaw CLI search provider is accepted", search_result)
            expect(results and results[0]["source"] == "pytorch.org", "OpenClaw CLI search results are normalized", results)
        finally:
            model_client_module._LOCAL_ENV_CACHE = None
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home
            if previous_key is not None:
                os.environ["MOONSHOT_API_KEY"] = previous_key

    print("provider configuration smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
