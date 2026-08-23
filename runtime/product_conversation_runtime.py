"""Durable, exact-scope Product Conversation ledger.

The Product Conversation surface is deliberately a small storage boundary.  It
does not decide whether Veyra should speak, execute, deliver, or expand a
permission.  Living Reaction decides ``suggest``; this runtime only records
that server-owned projection in a bound conversation.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from core.world_state import StateRevisionConflictError
from memory_bridge.scope import framed_sha256, normalize_scope_component


CONVERSATION_STATE_FILE = "product_conversation_state.json"
CONVERSATION_SCHEMA = "veyra.product_conversation_state.v1"
CONVERSATION_VIEW_SCHEMA = "veyra.product_conversation.v1"
CONVERSATION_LIST_SCHEMA = "veyra.product_conversation_list.v1"
CONVERSATION_MESSAGE_SCHEMA = "veyra.product_conversation_message.v1"


class ProductConversationError(RuntimeError):
    """Base Product Conversation error."""


class ProductConversationNotFound(ProductConversationError):
    """The conversation is absent or outside the caller's exact scope."""


class ProductConversationScopeError(ProductConversationError):
    """A stored conversation or binding violates its owner/session scope."""


class ProductConversationConflict(ProductConversationError):
    """An idempotency identity was reused with different semantics."""


class ProductConversationRevisionConflict(ProductConversationError):
    """A per-conversation compare-and-swap revision is stale."""


class ProductConversationStorageError(ProductConversationError):
    """The conversation document is corrupt or has reached capacity."""


def _authority() -> dict[str, bool]:
    return {
        "route": False,
        "risk": False,
        "tool": False,
        "agent": False,
        "execution": False,
        "delivery": False,
        "permission_expansion": False,
        "external_delivery": False,
    }


class ProductConversationRuntime:
    """Revisioned JSON conversation/message storage.

    The state store supplies process and filesystem atomicity.  This runtime
    adds bounded document validation, exact owner/session checks, per-
    conversation CAS, and idempotency scoped by owner/session.
    """

    STATE_FILE = CONVERSATION_STATE_FILE
    MAX_CONVERSATIONS = 500
    MAX_MESSAGES_PER_CONVERSATION = 200
    MAX_MESSAGES = 10_000
    MAX_TITLE_CHARS = 240
    MAX_TEXT_CHARS = 8_000
    MAX_METADATA_FIELDS = 24

    def __init__(
        self,
        state_store: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        max_conversations: int = MAX_CONVERSATIONS,
        max_messages_per_conversation: int = MAX_MESSAGES_PER_CONVERSATION,
        max_messages: int = MAX_MESSAGES,
    ) -> None:
        for name, value in {
            "max_conversations": max_conversations,
            "max_messages_per_conversation": max_messages_per_conversation,
            "max_messages": max_messages,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.state_store = state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_conversations = min(max_conversations, self.MAX_CONVERSATIONS)
        self.max_messages_per_conversation = min(
            max_messages_per_conversation,
            self.MAX_MESSAGES_PER_CONVERSATION,
        )
        self.max_messages = min(max_messages, self.MAX_MESSAGES)

    # ---------- public read API (must remain byte-pure) ----------

    def list_conversations(
        self,
        *,
        owner_id: str | None = None,
        user_id: str | None = None,
        session_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        owner = self._owner(owner_id if owner_id is not None else user_id)
        session = self._scope(session_id, "session_id")
        selected_limit = max(1, min(int(limit), self.max_conversations))
        state = self._read_state()
        conversations = state["conversations"]
        rows = [
            self._summary(row)
            for row in conversations.values()
            if isinstance(row, Mapping)
            and row.get("owner_id") == owner
            and row.get("session_id") == session
        ]
        rows.sort(
            key=lambda item: (
                str(item.get("updated_at") or ""),
                str(item.get("conversation_id") or ""),
            ),
            reverse=True,
        )
        return rows[:selected_limit]

    def get_conversation(
        self,
        conversation_id: str,
        *,
        owner_id: str | None = None,
        user_id: str | None = None,
        session_id: str,
    ) -> dict[str, Any] | None:
        owner = self._owner(owner_id if owner_id is not None else user_id)
        session = self._scope(session_id, "session_id")
        selected_id = self._conversation_id(conversation_id)
        state = self._read_state()
        row = state["conversations"].get(selected_id)
        if not isinstance(row, Mapping):
            return None
        self._assert_scope(row, owner, session)
        return self._detail(row)

    def get_reaction_message(
        self,
        reaction_id: str,
        *,
        owner_id: str | None = None,
        user_id: str | None = None,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Read the exact-scope proactive message for one reaction.

        This is intentionally a read-only lookup used to distinguish a
        historical Product Chat evaluation from feedback on the current
        reaction.  It does not repair indexes or mutate legacy rows.
        """

        owner = self._owner(owner_id if owner_id is not None else user_id)
        session = self._scope(session_id, "session_id")
        selected_reaction = self._text(reaction_id, "reaction_id", limit=240, required=True)
        state = self._read_state()
        reaction_key = self.reaction_key(
            owner_id=owner,
            session_id=session,
            reaction_id=selected_reaction,
        )
        indexed_message_id = state["reaction_index"].get(reaction_key)
        message_index = state["message_index"].get(
            self.message_key(
                owner_id=owner,
                session_id=session,
                message_id=str(indexed_message_id or ""),
            )
        ) if indexed_message_id else None
        conversation_id = message_index.get("conversation_id") if isinstance(message_index, Mapping) else None
        candidates: list[Mapping[str, Any]] = []
        if conversation_id:
            row = state["conversations"].get(str(conversation_id))
            if isinstance(row, Mapping):
                candidates.append(row)
        else:
            candidates.extend(
                row
                for row in state["conversations"].values()
                if isinstance(row, Mapping)
                and str(row.get("owner_id") or "") == owner
                and str(row.get("session_id") or "") == session
            )
        for row in candidates:
            self._assert_scope(row, owner, session)
            message = next(
                (
                    item
                    for item in row.get("messages") or []
                    if isinstance(item, Mapping)
                    and str(item.get("reaction_id") or "") == selected_reaction
                    and item.get("kind") == "proactive"
                    and item.get("source") == "living_reaction"
                ),
                None,
            )
            if isinstance(message, Mapping):
                return {
                    "conversation": self._detail(row),
                    "message": copy.deepcopy(dict(message)),
                    "reaction_id": selected_reaction,
                }
        return None

    # Explicit aliases make the storage boundary convenient for callers that
    # use the product vocabulary rather than the internal owner vocabulary.
    list = list_conversations
    get = get_conversation

    def binding_for_conversation(
        self,
        conversation_id: str,
        *,
        owner_id: str | None = None,
        user_id: str | None = None,
        session_id: str,
    ) -> dict[str, str] | None:
        """Read one exact-scope conversation binding without mutating state.

        The durable ``binding_id`` is an internal server value.  Callers that
        cross into model context must translate it to a catalog selector first;
        this helper intentionally returns the raw value only at the server
        runtime boundary.
        """

        conversation = self.get_conversation(
            conversation_id,
            owner_id=owner_id,
            user_id=user_id,
            session_id=session_id,
        )
        if not isinstance(conversation, Mapping):
            return None
        binding_type = str(conversation.get("binding_type") or "").strip().lower()
        binding_id = str(conversation.get("binding_id") or "").strip()
        if not binding_type and not binding_id:
            return None
        if binding_type != "situation" or not binding_id:
            raise ProductConversationStorageError("conversation binding is invalid")
        return {
            "binding_type": binding_type,
            "binding_id": binding_id,
        }

    def status(self) -> dict[str, Any]:
        """Return a pure, bounded health projection of the ledger."""

        state = self._read_state()
        conversations = state["conversations"]
        message_count = sum(
            len(row.get("messages") or [])
            for row in conversations.values()
            if isinstance(row, Mapping)
        )
        return {
            "schema_version": CONVERSATION_VIEW_SCHEMA,
            "status": "success",
            "conversation_count": len(conversations),
            "message_count": message_count,
            "capacity": {
                "max_conversations": self.max_conversations,
                "max_messages_per_conversation": self.max_messages_per_conversation,
                "max_messages": self.max_messages,
            },
            "mode": "record_only",
            "external_delivery": False,
            "authority": _authority(),
        }

    # ---------- conversation creation ----------

    def create_conversation(
        self,
        *,
        owner_id: str | None = None,
        user_id: str | None = None,
        session_id: str,
        binding_type: str | None = None,
        binding_id: str | None = None,
        title: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        owner = self._owner(owner_id if owner_id is not None else user_id)
        session = self._scope(session_id, "session_id")
        selected_binding_type, selected_binding_id = self._binding(
            binding_type,
            binding_id,
        )
        selected_title = self._text(title or "", "title", limit=self.MAX_TITLE_CHARS)
        selected_id = (
            self._conversation_id(conversation_id)
            if conversation_id
            else self.conversation_id_for_binding(
                owner_id=owner,
                session_id=session,
                binding_type=selected_binding_type,
                binding_id=selected_binding_id,
            )
            if selected_binding_type
            else f"conv_{uuid4().hex}"
        )
        result: dict[str, Any] = {}

        def update(state: dict[str, Any]) -> dict[str, Any]:
            self._prepare_state(state)
            existing = state["conversations"].get(selected_id)
            if isinstance(existing, dict):
                self._assert_scope(existing, owner, session)
                if not self._same_binding(existing, selected_binding_type, selected_binding_id):
                    raise ProductConversationConflict(
                        "conversation_id is already bound to different semantics"
                    )
                result.update({"status": "reused", "conversation": self._detail(existing)})
                return state
            binding_key = None
            if selected_binding_type:
                binding_key = self.binding_key(
                    owner_id=owner,
                    session_id=session,
                    binding_type=selected_binding_type,
                    binding_id=selected_binding_id or "",
                )
                indexed = state["binding_index"].get(binding_key)
                if not indexed:
                    # A manually migrated document may have the conversation
                    # row but not its secondary index.  Reuse the typed row
                    # rather than creating a second thread for one binding.
                    for candidate_id, candidate in state["conversations"].items():
                        if (
                            isinstance(candidate, dict)
                            and candidate.get("owner_id") == owner
                            and candidate.get("session_id") == session
                            and self._same_binding(
                                candidate,
                                selected_binding_type,
                                selected_binding_id,
                            )
                        ):
                            indexed = str(candidate_id)
                            state["binding_index"][binding_key] = indexed
                            break
                if indexed:
                    indexed_row = state["conversations"].get(str(indexed))
                    if not isinstance(indexed_row, dict):
                        raise ProductConversationStorageError(
                            "binding index points to a missing conversation"
                        )
                    self._assert_scope(indexed_row, owner, session)
                    if not self._same_binding(indexed_row, selected_binding_type, selected_binding_id):
                        raise ProductConversationStorageError(
                            "binding index points to an incompatible conversation"
                        )
                    result.update({"status": "reused", "conversation": self._detail(indexed_row)})
                    return state
            if len(state["conversations"]) >= self.max_conversations:
                raise ProductConversationStorageError("conversation capacity exhausted")
            now = self._now()
            row: dict[str, Any] = {
                "schema_version": CONVERSATION_VIEW_SCHEMA,
                "conversation_id": selected_id,
                "owner_id": owner,
                "session_id": session,
                "binding_type": selected_binding_type,
                "binding_id": selected_binding_id,
                "title": selected_title,
                "mode": "record_only",
                "external_delivery": False,
                "created_at": now,
                "updated_at": now,
                "revision": 1,
                "messages": [],
                "message_count": 0,
                "authority": _authority(),
            }
            state["conversations"][selected_id] = row
            if binding_key:
                state["binding_index"][binding_key] = selected_id
            state["conversation_count"] = len(state["conversations"])
            result.update({"status": "created", "conversation": self._detail(row)})
            return state

        self._mutate(update)
        return result["conversation"]

    def ensure_conversation(
        self,
        *,
        owner_id: str,
        session_id: str,
        binding_type: str | None = None,
        binding_id: str | None = None,
        title: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        return self.create_conversation(
            owner_id=owner_id,
            session_id=session_id,
            binding_type=binding_type,
            binding_id=binding_id,
            title=title,
            conversation_id=conversation_id,
        )

    def bind_conversation(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        session_id: str,
        binding_type: str,
        binding_id: str,
    ) -> dict[str, Any]:
        """Attach one typed Situation binding to an otherwise unbound thread."""

        owner = self._owner(owner_id)
        session = self._scope(session_id, "session_id")
        selected_id = self._conversation_id(conversation_id)
        selected_type, selected_binding = self._binding_static(binding_type, binding_id)
        result: dict[str, Any] = {}

        def update(state: dict[str, Any]) -> dict[str, Any]:
            self._prepare_state(state)
            row = state["conversations"].get(selected_id)
            if not isinstance(row, dict):
                raise ProductConversationNotFound("conversation not found")
            self._assert_scope(row, owner, session)
            existing_type = row.get("binding_type")
            existing_binding = row.get("binding_id")
            if existing_type or existing_binding:
                if self._same_binding(row, selected_type, selected_binding):
                    result["conversation"] = self._detail(row)
                    return state
                raise ProductConversationConflict("conversation is already bound")
            key = self.binding_key(
                owner_id=owner,
                session_id=session,
                binding_type=selected_type,
                binding_id=selected_binding,
            )
            prior_id = state["binding_index"].get(key)
            if prior_id and str(prior_id) != selected_id:
                raise ProductConversationConflict("Situation binding is already bound to another conversation")
            row["binding_type"] = selected_type
            row["binding_id"] = selected_binding
            row["updated_at"] = self._now()
            row["revision"] = self._revision(row.get("revision"), "conversation.revision") + 1
            state["binding_index"][key] = selected_id
            result["conversation"] = self._detail(row)
            return state

        self._mutate(update)
        return result["conversation"]

    # ---------- message recording ----------

    def append_message(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        session_id: str,
        message_id: str,
        role: str,
        text: str,
        kind: str = "foreground",
        source: str = "product",
        reaction_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        owner = self._owner(owner_id)
        session = self._scope(session_id, "session_id")
        selected_conversation = self._conversation_id(conversation_id)
        selected_message = self._message_id(message_id)
        selected_role = self._role(role)
        selected_text = self._text(text, "text", limit=self.MAX_TEXT_CHARS, required=True)
        selected_kind = self._text(kind, "kind", limit=64, required=True)
        selected_source = self._text(source, "source", limit=96, required=True)
        selected_reaction = self._text(reaction_id or "", "reaction_id", limit=240) or None
        selected_metadata = self._metadata(metadata)
        result: dict[str, Any] = {}

        def update(state: dict[str, Any]) -> dict[str, Any]:
            self._prepare_state(state)
            status, message, row = self._append_message_in_state(
                state,
                conversation_id=selected_conversation,
                owner_id=owner,
                session_id=session,
                message_id=selected_message,
                role=selected_role,
                text=selected_text,
                kind=selected_kind,
                source=selected_source,
                reaction_id=selected_reaction,
                metadata=selected_metadata,
                expected_revision=expected_revision,
            )
            result.update({"status": status, "message": message, "conversation": self._detail(row)})
            return state

        self._mutate(update)
        return result

    def update_reaction_feedback(
        self,
        reaction_id: str,
        *,
        owner_id: str,
        session_id: str,
        label: str,
        feedback_at: str | None = None,
    ) -> dict[str, Any]:
        """Close the feedback affordance on one proactive message.

        The reaction ledger owns the meaning of feedback.  This method only
        updates the already-recorded Product Conversation projection so a
        refresh cannot offer a second choice for the same message.  The
        message identity and digest are kept in sync, making the update
        idempotent and preventing a later label from silently replacing the
        first one.
        """

        owner = self._owner(owner_id)
        session = self._scope(session_id, "session_id")
        selected_reaction = self._text(reaction_id, "reaction_id", limit=240, required=True)
        selected_label = self._text(label, "feedback.label", limit=40, required=True).lower()
        selected_at = self._text(feedback_at or self._now(), "feedback_at", limit=80, required=True)
        result: dict[str, Any] = {}

        def update(state: dict[str, Any]) -> dict[str, Any]:
            self._prepare_state(state)
            reaction_key = self.reaction_key(
                owner_id=owner,
                session_id=session,
                reaction_id=selected_reaction,
            )
            indexed_message_id = state["reaction_index"].get(reaction_key)
            message_index = state["message_index"].get(
                self.message_key(
                    owner_id=owner,
                    session_id=session,
                    message_id=str(indexed_message_id or ""),
                )
            ) if indexed_message_id else None
            conversation_id = message_index.get("conversation_id") if isinstance(message_index, Mapping) else None
            row: dict[str, Any] | None = None
            message: dict[str, Any] | None = None
            if conversation_id:
                candidate = state["conversations"].get(str(conversation_id))
                if not isinstance(candidate, dict):
                    raise ProductConversationStorageError(
                        "reaction index points to a missing conversation"
                    )
                self._assert_scope(candidate, owner, session)
                row = candidate
                message = next(
                    (
                        item
                        for item in candidate.get("messages") or []
                        if isinstance(item, dict)
                        and str(item.get("reaction_id") or "") == selected_reaction
                    ),
                    None,
                )
            else:
                # Legacy rows may predate the secondary reaction index.  A
                # scoped scan repairs only the index, never the message
                # identity, and keeps historical feedback addressable.
                for candidate in state["conversations"].values():
                    if not isinstance(candidate, dict):
                        continue
                    if str(candidate.get("owner_id") or "") != owner or str(candidate.get("session_id") or "") != session:
                        continue
                    found = next(
                        (
                            item
                            for item in candidate.get("messages") or []
                            if isinstance(item, dict)
                            and str(item.get("reaction_id") or "") == selected_reaction
                        ),
                        None,
                    )
                    if found is not None:
                        row = candidate
                        message = found
                        state["reaction_index"][reaction_key] = str(found.get("message_id") or "")
                        break
            if row is None or message is None:
                result.update(
                    status="unavailable",
                    reason="proactive_message_not_found",
                    reaction_id=selected_reaction,
                )
                return state

            metadata = message.get("metadata")
            metadata = copy.deepcopy(metadata) if isinstance(metadata, Mapping) else {}
            prior_label = str(metadata.get("feedback_label") or "").strip().lower()
            feedback_available = metadata.get("feedback_available")
            if feedback_available is False:
                if prior_label == selected_label:
                    result.update(
                        status="duplicate",
                        message=copy.deepcopy(message),
                        conversation=self._detail(row),
                        reaction_id=selected_reaction,
                    )
                    return state
                raise ProductConversationConflict(
                    "proactive message feedback is already bound to a different label"
                )

            metadata["feedback_available"] = False
            metadata["feedback_label"] = selected_label
            metadata["feedback_at"] = selected_at
            message["metadata"] = metadata
            message["updated_at"] = self._now()
            conversation_id = str(row.get("conversation_id") or "")
            message_id = str(message.get("message_id") or "")
            state["message_index"][self.message_key(
                owner_id=owner,
                session_id=session,
                message_id=message_id,
            )] = {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "digest": self._message_digest(
                    conversation_id=conversation_id,
                    owner_id=owner,
                    session_id=session,
                    message_id=message_id,
                    role=message.get("role"),
                    text=message.get("text"),
                    kind=message.get("kind"),
                    source=message.get("source"),
                    reaction_id=message.get("reaction_id"),
                    metadata=metadata,
                ),
            }
            row["updated_at"] = self._now()
            row["revision"] = self._revision(row.get("revision"), "conversation.revision") + 1
            result.update(
                status="recorded",
                message=copy.deepcopy(message),
                conversation=self._detail(row),
                reaction_id=selected_reaction,
            )
            return state

        self._mutate(update)
        return result

    def _append_message_in_state(
        self,
        state: dict[str, Any],
        *,
        conversation_id: str,
        owner_id: str,
        session_id: str,
        message_id: str,
        role: str,
        text: str,
        kind: str,
        source: str,
        reaction_id: str | None,
        metadata: dict[str, Any],
        expected_revision: int | None = None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Append one prevalidated message to an in-memory state mutation."""

        row = state["conversations"].get(conversation_id)
        if not isinstance(row, dict):
            raise ProductConversationNotFound("conversation not found")
        self._assert_scope(row, owner_id, session_id)
        current_revision = self._revision(row.get("revision"), "conversation.revision")
        digest = self._message_digest(
            conversation_id=conversation_id,
            owner_id=owner_id,
            session_id=session_id,
            message_id=message_id,
            role=role,
            text=text,
            kind=kind,
            source=source,
            reaction_id=reaction_id,
            metadata=metadata,
        )
        message_key = self.message_key(
            owner_id=owner_id,
            session_id=session_id,
            message_id=message_id,
        )
        prior = state["message_index"].get(message_key)
        if prior is not None:
            if not isinstance(prior, dict) or prior.get("digest") != digest:
                raise ProductConversationConflict(
                    "message_id is already bound to different semantics"
                )
            existing = self._find_message(row, message_id)
            if existing is None:
                raise ProductConversationStorageError(
                    "message index points to a missing message"
                )
            return "duplicate", existing, row
        if expected_revision is not None and int(expected_revision) != current_revision:
            raise ProductConversationRevisionConflict(
                f"expected conversation revision {expected_revision}, got {current_revision}"
            )
        messages = row.get("messages")
        if not isinstance(messages, list):
            raise ProductConversationStorageError("conversation messages are corrupt")
        total_messages = int(state.get("message_count") or 0)
        if len(messages) >= self.max_messages_per_conversation:
            raise ProductConversationStorageError("conversation message capacity exhausted")
        if total_messages >= self.max_messages:
            raise ProductConversationStorageError("message capacity exhausted")
        now = self._now()
        message: dict[str, Any] = {
            "schema_version": CONVERSATION_MESSAGE_SCHEMA,
            "message_id": message_id,
            "conversation_id": conversation_id,
            "owner_id": owner_id,
            "session_id": session_id,
            "role": role,
            "kind": kind,
            "source": source,
            "text": text,
            "created_at": now,
            "updated_at": now,
            "reaction_id": reaction_id,
            "metadata": metadata,
            "record_only": True,
            "external_delivery": False,
            "authority": _authority(),
        }
        messages.append(message)
        row["messages"] = messages
        row["message_count"] = len(messages)
        row["updated_at"] = now
        row["revision"] = current_revision + 1
        state["message_index"][message_key] = {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "digest": digest,
        }
        if reaction_id:
            reaction_key = self.reaction_key(
                owner_id=owner_id,
                session_id=session_id,
                reaction_id=reaction_id,
            )
            state["reaction_index"][reaction_key] = message_id
        state["message_count"] = total_messages + 1
        return "recorded", copy.deepcopy(message), row

    def record_reaction(
        self,
        reaction: Mapping[str, Any],
        *,
        text: str | None = None,
    ) -> dict[str, Any]:
        """Record exactly one proactive assistant message for ``suggest``."""

        if not isinstance(reaction, Mapping):
            raise TypeError("reaction must be an object")
        if "disposition" not in reaction and isinstance(reaction.get("decision"), Mapping):
            reaction = reaction["decision"]
        disposition = self._text(reaction.get("disposition"), "disposition", limit=32).lower()
        if disposition != "suggest":
            return {
                "status": "ignored",
                "reason": "reaction_disposition_not_suggest",
                "disposition": disposition,
                "authority": _authority(),
            }
        owner = self._owner(reaction.get("owner_id") or reaction.get("user_id"))
        session = self._scope(reaction.get("session_id"), "session_id")
        situation_id = self._scope(reaction.get("situation_id"), "binding_id")
        reaction_id = self._scope(reaction.get("reaction_id"), "reaction_id")
        situation = reaction.get("situation")
        situation_title = (
            situation.get("title")
            if isinstance(situation, Mapping)
            else ""
        )
        conversation = self.ensure_conversation(
            owner_id=owner,
            session_id=session,
            binding_type="situation",
            binding_id=situation_id,
            title=self._text(
                reaction.get("title") or situation_title or "",
                "title",
                limit=self.MAX_TITLE_CHARS,
            ),
        )
        conversation_id = str(conversation["conversation_id"])
        what_happened = self._text(reaction.get("what_happened"), "what_happened", limit=1600)
        why_it_matters = self._text(reaction.get("why_it_matters"), "why_it_matters", limit=1200)
        why_now = self._text(reaction.get("why_now"), "why_now", limit=1000)
        suggested = self._text(reaction.get("suggested_next_step"), "suggested_next_step", limit=1000)
        rendered = text
        if not rendered:
            # Keep the legacy sparse reaction shape byte-compatible while the
            # full V1 reaction renders the complete explanation for Product
            # Chat.  Cognitive candidates always populate all four fields.
            if what_happened or why_it_matters or why_now:
                rendered = "\n".join(
                    value
                    for value in (
                        what_happened,
                        f"为什么重要：{why_it_matters}" if why_it_matters else "",
                        f"为什么现在：{why_now}" if why_now else "",
                        f"建议下一步：{suggested}" if suggested else "",
                    )
                    if value
                )
            else:
                rendered = suggested
        selected_text = self._text(rendered, "suggested_next_step", limit=self.MAX_TEXT_CHARS, required=True)
        message_id = "proactive_" + hashlib.sha256(
            f"veyra.product_conversation.reaction.v1\0{owner}\0{session}\0{reaction_id}".encode("utf-8")
        ).hexdigest()[:32]
        proactive_metadata = {
            "disposition": "suggest",
            "reaction_id": reaction_id,
            "situation_id": situation_id,
            "situation_revision": int(reaction.get("situation_revision") or 1),
            "category": self._text(reaction.get("category"), "category", limit=120),
            "attention_candidate_id": self._text(
                reaction.get("attention_candidate_id"),
                "attention_candidate_id",
                limit=240,
            ) or None,
            "fact_vs_inference": copy.deepcopy(reaction.get("fact_vs_inference") or {"facts": [], "inferences": []}),
            "feedback_token": reaction.get("reaction_token"),
            "attention_trigger": self._text(reaction.get("attention_trigger"), "none", limit=64),
            "feedback_available": True,
            "record_only": True,
            "external_delivery": False,
        }
        if str(reaction.get("attention_trigger") or "") == "cognitive_hypothesis":
            proactive_metadata["epistemic_status"] = "hypothesis"
        result = self.append_message(
            conversation_id,
            owner_id=owner,
            session_id=session,
            message_id=message_id,
            role="assistant",
            text=selected_text,
            kind="proactive",
            source="living_reaction",
            reaction_id=reaction_id,
            metadata=proactive_metadata,
        )
        # append_message owns the single atomic idempotency decision.  Avoid a
        # separate read-before-write reaction check, which can race with an
        # identical worker and observe a stale conversation snapshot.
        return {
            "status": result["status"],
            "conversation": result["conversation"],
            "message": result["message"],
            "reaction_id": reaction_id,
            "authority": _authority(),
        }

    record_proactive_reaction = record_reaction
    append_proactive_message = record_reaction

    def record_foreground_turn(
        self,
        *,
        owner_id: str,
        session_id: str,
        text: str,
        message_id: str,
        response: str | None = None,
        responses: Sequence[str] | None = None,
        conversation_id: str | None = None,
        situation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a user turn and final Veyra response(s) idempotently."""

        owner = self._owner(owner_id)
        session = self._scope(session_id, "session_id")
        selected_message_id = self._message_id(message_id)
        selected_situation = self._scope(situation_id, "binding_id") if situation_id else None
        selected_conversation_id = self._conversation_id(conversation_id) if conversation_id else None
        selected_text = self._text(text, "text", limit=self.MAX_TEXT_CHARS, required=True)
        selected_metadata = self._metadata(metadata)
        response_values = list(responses or ([] if response is None else [response]))
        response_values = [
            value_text
            for value in response_values
            if (value_text := self._text(value, "response", limit=self.MAX_TEXT_CHARS))
        ]
        if selected_conversation_id is None:
            selected_conversation_id = self._existing_foreground_conversation_id(
                owner=owner,
                session=session,
                message_id=selected_message_id,
                text=selected_text,
                metadata=selected_metadata,
            )
        if selected_conversation_id:
            current = self.get_conversation(
                selected_conversation_id,
                owner_id=owner,
                session_id=session,
            )
            if current is None:
                raise ProductConversationNotFound("conversation not found")
            if (
                selected_situation
                and current.get("binding_type") == "situation"
                and current.get("binding_id") != selected_situation
            ):
                raise ProductConversationConflict(
                    "conversation is bound to a different Situation"
                )
            if (
                selected_situation
                and not current.get("binding_type")
                and not current.get("binding_id")
            ):
                try:
                    current = self.bind_conversation(
                        selected_conversation_id,
                        owner_id=owner,
                        session_id=session,
                        binding_type="situation",
                        binding_id=selected_situation,
                    )
                except ProductConversationConflict:
                    # The user chose this conversation explicitly.  If a
                    # proactive worker won the Situation binding race, keep
                    # the foreground turn in A instead of switching identity
                    # to another thread.
                    current = self.get_conversation(
                        selected_conversation_id,
                        owner_id=owner,
                        session_id=session,
                    ) or current
                    if current.get("binding_type") and not self._same_binding(
                        current,
                        "situation",
                        selected_situation,
                    ):
                        raise ProductConversationConflict(
                            "conversation was bound to a different Situation"
                        )
        else:
            current = self.ensure_conversation(
                owner_id=owner,
                session_id=session,
                binding_type="situation" if selected_situation else None,
                binding_id=selected_situation,
                title=self._text(text, "title", limit=self.MAX_TITLE_CHARS),
            )
        selected_conversation_id = str(current["conversation_id"])
        message_specs: list[dict[str, Any]] = [
            {
                "message_id": selected_message_id,
                "role": "user",
                "text": selected_text,
                "kind": "foreground",
                "source": "user",
                "reaction_id": None,
                "metadata": selected_metadata,
            }
        ]
        for index, value_text in enumerate(response_values):
            response_id = self.foreground_response_message_id(
                owner_id=owner,
                session_id=session,
                message_id=selected_message_id,
                index=index,
            )
            message_specs.append(
                {
                    "message_id": response_id,
                    "role": "assistant",
                    "text": value_text,
                    "kind": "foreground",
                    "source": "veyra",
                    "reaction_id": None,
                    "metadata": {"response_index": index, "record_only": True},
                }
            )
        appended: list[dict[str, Any]] = []
        statuses: list[str] = []
        final_row: dict[str, Any] | None = None

        def update(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal final_row
            self._prepare_state(state)
            for spec in message_specs:
                status, message, row = self._append_message_in_state(
                    state,
                    conversation_id=selected_conversation_id,
                    owner_id=owner,
                    session_id=session,
                    **spec,
                )
                statuses.append(status)
                appended.append(message)
                final_row = row
            return state

        self._mutate(update)
        if final_row is None:
            raise ProductConversationStorageError("conversation disappeared after append")
        detail = self._detail(final_row)
        return {
            "status": "duplicate" if all(item == "duplicate" for item in statuses) else "recorded",
            "conversation_id": selected_conversation_id,
            "conversation": detail,
            "messages": appended,
            "authority": _authority(),
        }

    def record_foreground_completion(
        self,
        *,
        owner_id: str,
        session_id: str,
        text: str,
        message_id: str,
        conversation_id: str,
        response: str | None = None,
        responses: Sequence[str] | None = None,
        situation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append only the assistant tail for an already admitted user turn.

        This is intentionally narrower than ``record_foreground_turn``: a
        completion hook must carry the server-selected conversation identity
        and may never create a new user message if the admission ledger is
        missing.  The underlying turn mutation remains idempotent, so a retry
        after a hook or delivery failure appends the assistant tail once.
        """

        selected_conversation_id = self._conversation_id(conversation_id)
        owner = self._owner(owner_id)
        session = self._scope(session_id, "session_id")
        selected_text = self._text(text, "text", limit=self.MAX_TEXT_CHARS, required=True)
        selected_message_id = self._message_id(message_id)
        selected_metadata = self._metadata(metadata)
        admitted_conversation_id = self._existing_foreground_conversation_id(
            owner=owner,
            session=session,
            message_id=selected_message_id,
            text=selected_text,
            metadata=selected_metadata,
        )
        if admitted_conversation_id != selected_conversation_id:
            raise ProductConversationConflict(
                "foreground completion requires the server-admitted conversation"
            )
        binding = self.binding_for_conversation(
            selected_conversation_id,
            owner_id=owner,
            session_id=session,
        )
        server_diagnostic: dict[str, Any] | None = None
        public_diagnostic: dict[str, str] | None = None
        if (
            situation_id
            and isinstance(binding, Mapping)
            and binding.get("binding_type") == "situation"
            and str(binding.get("binding_id") or "") != str(situation_id)
        ):
            expected_binding = str(binding.get("binding_id") or "")
            observed_binding = str(situation_id)
            # Keep the admitted thread identity authoritative.  The model's
            # Situation projection is still retained in its own state path;
            # it must not rebind a Product Conversation during completion.
            situation_id = None
            public_diagnostic = {
                "code": "conversation_binding_mismatch",
                "status": "assistant_recorded_on_admitted_conversation",
            }
            server_diagnostic = {
                **public_diagnostic,
                "binding_type": "situation",
                "expected_digest": self._binding_digest(expected_binding),
                "observed_digest": self._binding_digest(observed_binding),
            }
        result = self.record_foreground_turn(
            owner_id=owner,
            session_id=session,
            text=selected_text,
            message_id=selected_message_id,
            response=response,
            responses=responses,
            conversation_id=selected_conversation_id,
            situation_id=situation_id,
            metadata=selected_metadata,
        )
        result["assistant_message_count"] = sum(
            1
            for message in result.get("messages") or []
            if isinstance(message, Mapping) and message.get("role") == "assistant"
        )
        if public_diagnostic is not None:
            result["diagnostic"] = public_diagnostic
        if server_diagnostic is not None:
            # This field is for server-side tracing only.  API adapters must
            # not forward it to the browser or external channel.
            result["_server_diagnostic"] = server_diagnostic
        return result


    # Friendly aliases for integration callers.
    record_user_turn = record_foreground_turn
    append_foreground_turn = record_foreground_turn

    def _existing_foreground_conversation_id(
        self,
        *,
        owner: str,
        session: str,
        message_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> str | None:
        """Recover a prior user-first admission for an idempotent retry."""

        state = self._read_state()
        message_key = self.message_key(
            owner_id=owner,
            session_id=session,
            message_id=message_id,
        )
        prior = state["message_index"].get(message_key)
        if prior is None:
            return None
        if not isinstance(prior, dict) or prior.get("message_id") != message_id:
            raise ProductConversationConflict("message index identity is invalid")
        conversation_id = str(prior.get("conversation_id") or "")
        row = state["conversations"].get(conversation_id)
        if not isinstance(row, dict):
            raise ProductConversationStorageError("message index points to a missing conversation")
        self._assert_scope(row, owner, session)
        existing = self._find_message(row, message_id)
        if not isinstance(existing, dict):
            raise ProductConversationStorageError("message index points to a missing message")
        if (
            existing.get("role") != "user"
            or existing.get("kind") != "foreground"
            or existing.get("source") != "user"
            or existing.get("reaction_id") is not None
            or existing.get("text") != text
            or existing.get("metadata") != metadata
        ):
            raise ProductConversationConflict(
                "message_id is already bound to different foreground semantics"
            )
        digest = self._message_digest(
            conversation_id=conversation_id,
            owner_id=owner,
            session_id=session,
            message_id=message_id,
            role="user",
            text=text,
            kind="foreground",
            source="user",
            reaction_id=None,
            metadata=metadata,
        )
        if prior.get("digest") != digest:
            raise ProductConversationConflict(
                "message_id foreground digest does not match the stored admission"
            )
        return conversation_id

    # ---------- deterministic typed identities ----------

    @classmethod
    def conversation_id_for_binding(
        cls,
        *,
        owner_id: str,
        session_id: str,
        binding_type: str,
        binding_id: str,
    ) -> str:
        selected_type, selected_id = cls._binding_static(binding_type, binding_id)
        digest = framed_sha256(
            "veyra.product_conversation.binding.v1",
            owner_id,
            session_id,
            selected_type,
            selected_id,
        )
        return f"conv_{digest[:32]}"

    @classmethod
    def binding_key(
        cls,
        *,
        owner_id: str,
        session_id: str,
        binding_type: str,
        binding_id: str,
    ) -> str:
        selected_type, selected_id = cls._binding_static(binding_type, binding_id)
        return framed_sha256(
            "veyra.product_conversation.binding_index.v1",
            owner_id,
            session_id,
            selected_type,
            selected_id,
        )

    @staticmethod
    def message_key(*, owner_id: str, session_id: str, message_id: str) -> str:
        return framed_sha256(
            "veyra.product_conversation.message_index.v1",
            owner_id,
            session_id,
            message_id,
        )

    @classmethod
    def foreground_response_message_id(
        cls,
        *,
        owner_id: str,
        session_id: str,
        message_id: str,
        index: int,
    ) -> str:
        return "response_" + hashlib.sha256(
            f"veyra.product_conversation.response.v1\0{owner_id}\0{session_id}\0{message_id}\0{index}".encode("utf-8")
        ).hexdigest()[:32]

    def has_foreground_completion(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        session_id: str,
        message_id: str,
    ) -> bool:
        """Return whether one foreground assistant response already exists."""

        owner = self._owner(owner_id)
        session = self._scope(session_id, "session_id")
        selected_conversation = self._conversation_id(conversation_id)
        selected_message = self._message_id(message_id)
        conversation = self.get_conversation(
            selected_conversation,
            owner_id=owner,
            session_id=session,
        )
        if conversation is None:
            return False
        response_id = self.foreground_response_message_id(
            owner_id=owner,
            session_id=session,
            message_id=selected_message,
            index=0,
        )
        return any(
            isinstance(item, Mapping)
            and str(item.get("message_id") or "") == response_id
            and item.get("role") == "assistant"
            and item.get("kind") == "foreground"
            and item.get("source") == "veyra"
            for item in conversation.get("messages") or []
        )

    @staticmethod
    def reaction_key(*, owner_id: str, session_id: str, reaction_id: str) -> str:
        return framed_sha256(
            "veyra.product_conversation.reaction_index.v1",
            owner_id,
            session_id,
            reaction_id,
        )

    # ---------- internal state / validation ----------

    def _read_state(self) -> dict[str, Any]:
        raw = self.state_store.read_json(self.STATE_FILE)
        state = copy.deepcopy(raw) if isinstance(raw, dict) else {}
        self._prepare_state(state)
        self._validate_state(state)
        return state

    def _mutate(self, mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            self._prepare_state(state)
            mutated = mutator(state)
            self._validate_state(mutated)
            return mutated

        try:
            return self.state_store.mutate_json(self.STATE_FILE, apply)
        except StateRevisionConflictError as exc:
            raise ProductConversationRevisionConflict(str(exc)) from exc

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema_version": CONVERSATION_SCHEMA,
            "conversations": {},
            "binding_index": {},
            "message_index": {},
            "reaction_index": {},
            "conversation_count": 0,
            "message_count": 0,
            "authority": _authority(),
        }

    @classmethod
    def _prepare_state(cls, state: dict[str, Any]) -> None:
        if state.get("_state_corrupt"):
            raise ProductConversationStorageError("conversation state is corrupt")
        if not state:
            state.update(cls._empty_state())
            return
        if state.get("schema_version") not in {None, CONVERSATION_SCHEMA}:
            raise ProductConversationStorageError("unsupported conversation schema")
        state.setdefault("schema_version", CONVERSATION_SCHEMA)
        for key in ("conversations", "binding_index", "message_index", "reaction_index"):
            if key not in state:
                state[key] = {}
        state.setdefault("conversation_count", len(state.get("conversations", {})))
        state.setdefault("message_count", 0)
        state.setdefault("authority", _authority())

    def _validate_state(self, state: Mapping[str, Any]) -> None:
        if state.get("schema_version") != CONVERSATION_SCHEMA:
            raise ProductConversationStorageError("conversation schema is invalid")
        conversations = state.get("conversations")
        indexes = [state.get(key) for key in ("binding_index", "message_index", "reaction_index")]
        if not isinstance(conversations, Mapping) or not all(isinstance(item, Mapping) for item in indexes):
            raise ProductConversationStorageError("conversation indexes are invalid")
        if len(conversations) > self.max_conversations:
            raise ProductConversationStorageError("conversation capacity exceeded")
        message_count = 0
        for conversation_id, row in conversations.items():
            if not isinstance(row, Mapping):
                raise ProductConversationStorageError("conversation row is invalid")
            if str(row.get("conversation_id") or "") != str(conversation_id):
                raise ProductConversationStorageError("conversation identity is invalid")
            self._owner(row.get("owner_id"))
            self._scope(row.get("session_id"), "session_id")
            self._conversation_id(conversation_id)
            self._revision(row.get("revision"), "conversation.revision")
            self._binding(row.get("binding_type"), row.get("binding_id"))
            messages = row.get("messages")
            if not isinstance(messages, list) or len(messages) > self.max_messages_per_conversation:
                raise ProductConversationStorageError("conversation messages are invalid")
            message_count += len(messages)
            seen: set[str] = set()
            for message in messages:
                if not isinstance(message, Mapping):
                    raise ProductConversationStorageError("conversation message is invalid")
                message_id = self._message_id(message.get("message_id"))
                if message_id in seen:
                    raise ProductConversationStorageError("duplicate message identity")
                seen.add(message_id)
                if str(message.get("conversation_id") or "") != str(conversation_id):
                    raise ProductConversationScopeError("message conversation scope mismatch")
                if str(message.get("owner_id") or "") != str(row.get("owner_id") or ""):
                    raise ProductConversationScopeError("message owner scope mismatch")
                if str(message.get("session_id") or "") != str(row.get("session_id") or ""):
                    raise ProductConversationScopeError("message session scope mismatch")
                self._role(message.get("role"))
                self._text(message.get("text"), "message.text", required=True, limit=self.MAX_TEXT_CHARS)
        if message_count > self.max_messages:
            raise ProductConversationStorageError("message capacity exceeded")
        if int(state.get("conversation_count") or 0) != len(conversations):
            raise ProductConversationStorageError("conversation count is invalid")
        if int(state.get("message_count") or 0) != message_count:
            raise ProductConversationStorageError("message count is invalid")
        binding_index = state.get("binding_index")
        for key, conversation_id in binding_index.items():
            row = conversations.get(str(conversation_id))
            if not isinstance(row, Mapping):
                raise ProductConversationStorageError("binding index target is missing")
            if row.get("binding_type") != "situation" or not row.get("binding_id"):
                raise ProductConversationStorageError("binding index target is untyped")
            expected_key = self.binding_key(
                owner_id=str(row.get("owner_id") or ""),
                session_id=str(row.get("session_id") or ""),
                binding_type="situation",
                binding_id=str(row.get("binding_id") or ""),
            )
            if str(key) != expected_key:
                raise ProductConversationStorageError("binding index identity is invalid")
        message_ids = {
            (str(conversation_id), str(message.get("message_id") or ""))
            for conversation_id, row in conversations.items()
            for message in (row.get("messages") or [])
            if isinstance(message, Mapping)
        }
        for key, value in state.get("message_index", {}).items():
            if (
                not isinstance(value, Mapping)
                or (str(value.get("conversation_id") or ""), str(value.get("message_id") or ""))
                not in message_ids
            ):
                raise ProductConversationStorageError("message index target is missing")
        for key, message_id in state.get("reaction_index", {}).items():
            if not isinstance(message_id, str):
                raise ProductConversationStorageError("reaction index value is invalid")
            if not any((str(conversation_id), message_id) in message_ids for conversation_id in conversations):
                raise ProductConversationStorageError("reaction index target is missing")

    @staticmethod
    def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
        messages = row.get("messages") if isinstance(row.get("messages"), list) else []
        last = messages[-1] if messages else {}
        preview = str(last.get("text") or "")[:240] if isinstance(last, Mapping) else ""
        return {
            "schema_version": CONVERSATION_VIEW_SCHEMA,
            "conversation_id": row.get("conversation_id"),
            "id": row.get("conversation_id"),
            "owner_id": row.get("owner_id"),
            "session_id": row.get("session_id"),
            "binding_type": row.get("binding_type"),
            "binding_id": row.get("binding_id"),
            "title": row.get("title") or preview,
            "preview": preview,
            "last_message": preview,
            "message_count": len(messages),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "mode": "record_only",
            "external_delivery": False,
            "authority": _authority(),
        }

    @classmethod
    def _detail(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        summary = cls._summary(row)
        summary.update(
            {
                "revision": row.get("revision"),
                "messages": copy.deepcopy(row.get("messages") or []),
            }
        )
        return summary

    @staticmethod
    def _find_message(row: Mapping[str, Any], message_id: str) -> dict[str, Any] | None:
        for message in row.get("messages") or []:
            if isinstance(message, Mapping) and str(message.get("message_id") or "") == message_id:
                return copy.deepcopy(dict(message))
        return None

    @classmethod
    def _same_binding(cls, row: Mapping[str, Any], binding_type: str | None, binding_id: str | None) -> bool:
        return row.get("binding_type") == binding_type and row.get("binding_id") == binding_id

    @staticmethod
    def _message_digest(**values: Any) -> str:
        encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _binding_digest(binding_id: str) -> str:
        """Return a bounded diagnostic digest without exposing a binding ID."""

        return hashlib.sha256(
            f"veyra.product_conversation.binding-diagnostic.v1\0{binding_id}".encode(
                "utf-8"
            )
        ).hexdigest()[:16]

    def _assert_scope(self, row: Mapping[str, Any], owner: str, session: str) -> None:
        if str(row.get("owner_id") or "") != owner or str(row.get("session_id") or "") != session:
            raise ProductConversationNotFound("conversation not found")

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("conversation clock must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _owner(value: Any) -> str:
        return normalize_scope_component(value, "owner_id")

    @staticmethod
    def _scope(value: Any, field: str) -> str:
        return normalize_scope_component(value, field)

    @staticmethod
    def _text(value: Any, field: str, *, required: bool = False, limit: int = 600) -> str:
        selected = str(value or "").strip()
        if required and not selected:
            raise ValueError(f"{field} is required")
        if len(selected) > limit:
            raise ValueError(f"{field} exceeds {limit} characters")
        return selected

    @classmethod
    def _conversation_id(cls, value: Any) -> str:
        return cls._text(value, "conversation_id", required=True, limit=240)

    @classmethod
    def _message_id(cls, value: Any) -> str:
        return cls._text(value, "message_id", required=True, limit=240)

    @classmethod
    def _role(cls, value: Any) -> str:
        role = cls._text(value, "role", required=True, limit=32).lower()
        if role not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        return role

    @classmethod
    def _metadata(cls, value: Mapping[str, Any] | None) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError("metadata must be an object")
        if len(value) > cls.MAX_METADATA_FIELDS:
            raise ValueError("metadata has too many fields")
        try:
            encoded = json.dumps(dict(value), ensure_ascii=False, allow_nan=False, default=str)
            if len(encoded) > 8_000:
                raise ValueError("metadata is too large")
            return copy.deepcopy(dict(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata is not JSON-compatible") from exc

    @classmethod
    def _binding_static(cls, binding_type: Any, binding_id: Any) -> tuple[str, str]:
        selected_type = cls._text(binding_type, "binding_type", required=True, limit=64).lower()
        if selected_type != "situation":
            raise ValueError("only situation bindings are supported")
        selected_id = cls._scope(binding_id, "binding_id")
        return selected_type, selected_id

    @classmethod
    def _binding(cls, binding_type: Any, binding_id: Any) -> tuple[str | None, str | None]:
        if binding_type in (None, "") and binding_id in (None, ""):
            return None, None
        if binding_type in (None, "") or binding_id in (None, ""):
            raise ValueError("binding_type and binding_id must be supplied together")
        selected_type, selected_id = cls._binding_static(binding_type, binding_id)
        return selected_type, selected_id

    @staticmethod
    def _revision(value: Any, field: str) -> int:
        selected = 1 if value is None else value
        if isinstance(selected, bool) or not isinstance(selected, int) or selected < 1:
            raise ProductConversationStorageError(f"{field} must be a positive integer")
        return selected


__all__ = [
    "ProductConversationRuntime",
    "ProductConversationError",
    "ProductConversationNotFound",
    "ProductConversationScopeError",
    "ProductConversationConflict",
    "ProductConversationRevisionConflict",
    "ProductConversationStorageError",
    "CONVERSATION_STATE_FILE",
]
