from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_FENCES_GUARD = threading.Lock()
_AGENT_TRANSPORT_FENCES: dict[str, threading.RLock] = {}
_AGENT_TRANSPORT_CALLS_INFLIGHT: set[str] = set()
_AGENT_TRANSPORT_DOMAIN = "agent_transport"


def _root_key(state_store: Any) -> str:
    root = getattr(state_store, "root", None)
    return str(Path(root).resolve()) if root is not None else str(id(state_store))


@contextmanager
def agent_transport_authority_fence(
    state_store: Any,
) -> Iterator[None]:
    """Serialize governed Agent admission with bounded transport maintenance.

    WorldStateStore already enforces one writable process per state root. This
    process-local fence therefore covers every supported governed dispatch
    boundary without holding the state RLock across a Gateway call.
    """

    transaction = getattr(state_store, "authority_transaction", None)
    if callable(transaction):
        with transaction(_AGENT_TRANSPORT_DOMAIN):
            yield
        return

    key = _root_key(state_store)
    with _FENCES_GUARD:
        fence = _AGENT_TRANSPORT_FENCES.setdefault(key, threading.RLock())
    with fence:
        yield


def mark_agent_transport_call_inflight(state_store: Any) -> bool:
    """Reserve the one bounded transport-maintenance call for this state root."""

    marker = getattr(state_store, "mark_authority_call_inflight", None)
    if callable(marker):
        return bool(marker(_AGENT_TRANSPORT_DOMAIN))
    key = _root_key(state_store)
    with _FENCES_GUARD:
        if key in _AGENT_TRANSPORT_CALLS_INFLIGHT:
            return False
        _AGENT_TRANSPORT_CALLS_INFLIGHT.add(key)
        return True


def clear_agent_transport_call_inflight(state_store: Any) -> None:
    """Release a transport call reservation after its worker actually exits."""

    clearer = getattr(state_store, "clear_authority_call_inflight", None)
    if callable(clearer):
        clearer(_AGENT_TRANSPORT_DOMAIN)
        return
    key = _root_key(state_store)
    with _FENCES_GUARD:
        _AGENT_TRANSPORT_CALLS_INFLIGHT.discard(key)


def agent_transport_call_inflight(state_store: Any) -> bool:
    """Return whether a transport worker can still produce a late effect."""

    checker = getattr(state_store, "authority_call_inflight", None)
    if callable(checker):
        return bool(checker(_AGENT_TRANSPORT_DOMAIN))
    key = _root_key(state_store)
    with _FENCES_GUARD:
        return key in _AGENT_TRANSPORT_CALLS_INFLIGHT
