import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

from core.awareness_loop import AwarenessLoop
from core.runtime_entity import RuntimeEntity
from core.world_state import WorldStateStore
from interface.intake_gateway import IntakeGateway


def main() -> None:
    text = input("Veyra> ").strip()
    if not text:
        return
    base_url = os.getenv("VEYRA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    request = Request(
        f"{base_url}/events/message",
        data=json.dumps(
            {
                "text": text,
                "channel": "cli",
                "user_id": "local-user",
                "session_id": "local-cli",
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            print(json.loads(response.read().decode("utf-8") or "{}"))
            return
    except (URLError, TimeoutError, OSError):
        pass

    state_store = WorldStateStore(exclusive_writer=True, writer_owner="veyra-cli-offline")
    runtime = RuntimeEntity(state_store)
    gateway = IntakeGateway(AwarenessLoop(state_store, runtime), state_store=state_store)
    print(gateway.receive_text(text).to_dict())


if __name__ == "__main__":
    main()
