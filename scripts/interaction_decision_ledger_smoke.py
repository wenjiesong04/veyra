#!/usr/bin/env python3
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context_scope import tenant_scope_storage_key  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from runtime.suggestion_outbox import SuggestionOutbox  # noqa: E402


USER = "ledger-owner"
SESSION = "ledger-session"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.endswith(".lock"):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    with TemporaryDirectory(prefix="veyra-interaction-ledger-") as raw:
        root = Path(raw) / "state"
        store = WorldStateStore(root)
        outbox = SuggestionOutbox(store)
        parent = {
            "user_id": USER,
            "session_scope_keys": [tenant_scope_storage_key(USER, SESSION)],
            "general_situation_id": "gsit-interaction-ledger",
            "parent_revision": 1,
        }
        base = {
            "general_situation_id": parent["general_situation_id"],
            "parent_revision": 1,
            "authority": outbox._authority_boundary(),
            "eligible": False,
        }
        wait = outbox.consider(
            parent,
            {**base, "hypothesis_status": "candidate"},
            user_id=USER,
            session_id=SESSION,
        )
        ask = outbox.consider(
            parent,
            {
                **base,
                "hypothesis_status": "confirmed",
                "interaction_gap": {
                    "kind": "owner_question",
                    "gap_id": "gap_ledger_missing_input",
                    "answerable": True,
                },
            },
            user_id=USER,
            session_id=SESSION,
        )
        silent = outbox.consider(
            parent,
            {**base, "hypothesis_status": "contradicted"},
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            wait.get("decision_disposition") == "wait"
            and ask.get("decision_disposition") == "ask"
            and silent.get("decision_disposition") == "silent"
            and all(
                isinstance(item.get("interaction_decision"), dict)
                for item in (wait, ask, silent)
            ),
            "wait/ask/silent decisions are returned with durable references",
            {"wait": wait, "ask": ask, "silent": silent},
        )
        state = store.read_json(SuggestionOutbox.STATE_FILE)
        decisions = state.get("interaction_decisions") or {}
        expect(
            state.get("interaction_decision_count") == 3
            and len(decisions) == 3
            and all(
                item.get("schema_version") == SuggestionOutbox.DECISION_SCHEMA_VERSION
                and item.get("user_id") == USER
                and item.get("session_id") == SESSION
                and item.get("proposal_id") is None
                and item.get("delivery_disposition") == "none"
                and not any(item.get("authority", {}).values())
                for item in decisions.values()
            ),
            "decision ledger is exact-owner, proposal-free, and non-authorizing",
            decisions,
        )
        before_get = tree_digest(root)
        status = outbox.status()
        status_again = outbox.status()
        inbox = outbox.list_inbox(user_id=USER, session_id=SESSION)
        after_get = tree_digest(root)
        expect(
            status["interaction_decision_count"] == 3
            and status == status_again
            and inbox["count"] == 0
            and before_get == after_get,
            "status and owner inbox GETs remain byte-pure after ledger writes",
            {"status": status, "inbox": inbox},
        )
        print("Interaction decision ledger smoke passed: 3/3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
