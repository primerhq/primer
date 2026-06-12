"""Chat + ChatMessage storage models for the M6 WebSocket chat surface.

A :class:`Chat` is lighter than a :class:`Session`: no workspace, no
graph binding, no per-call signals beyond the M1 yield protocol.
It's a long-lived row that pairs one agent with an append-only
conversation history (:class:`ChatMessage` rows).

Unlike :class:`Session`, a chat NEVER parks: when a chat agent
invokes a yielding tool the turn ends awaiting the human's reply,
recorded purely in-conversation via :attr:`Chat.pending_tool_call`
and the message log. There is no park/resume machinery on the chat
surface.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md`` §8.5.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from primer.model.common import Identifiable


ChatStatus = Literal["active", "ended"]
"""Chat lifecycle states.

* ``active`` — accepting user messages; the next user_message may
  trigger another agent turn.
* ``ended`` — terminal. Either the operator explicitly ended it via
  ``DELETE /v1/chats/{id}`` or an unrecoverable error pinned it.
  No further messages are accepted.
"""


ChatMessageKind = Literal[
    "user_message",
    "assistant_token",
    "tool_call",
    "tool_result",
    # Legacy-unused on the chat soft-yield path (chats never park, so no
    # row is ever written with these kinds). Retained because
    # dispatch._find_next_user_message still lists "yielded" among its
    # recognized terminal kinds; dropping it would desync that set.
    "yielded",
    "resumed",
    "done",
    "cancelled",
    "error",
    "compaction_marker",
]
"""The wire-level message kinds emitted by the chat executor.

The set mirrors the M6 spec (§8.5). Each row in ``chat_messages``
carries the kind plus a kind-specific ``payload`` JSON blob — see
the spec for the per-kind schema.
"""


class Chat(Identifiable):
    """A user-driven conversation with a single agent.

    Persisted as a top-level entity (not nested under workspace) so
    the WebSocket surface can address it directly. A chat never
    parks; a yielding tool invocation is held in-conversation via
    :attr:`pending_tool_call` rather than a park slot.
    """

    agent_id: str = Field(
        ...,
        min_length=1,
        description=(
            "The chat's CURRENT agent - it handles the next turn. Switchable "
            "mid-chat via POST /v1/chats/{id}/agent, which auto-resolves any "
            "pending gate first. The agent + its system prompt are resolved "
            "fresh each turn and never stored in history, so switching keeps "
            "the full conversation as shared context."
        ),
    )
    created_at: datetime = Field(...)
    status: ChatStatus = Field(default="active")
    title: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Human-friendly title derived from the first user_message "
            "text. Stamped once by :class:`primer.chat.executor.ChatTurnRunner` "
            "on the first turn and never overwritten — the conversation "
            "evolves but the originating intent stays in the list view. "
            "``None`` on chats that haven't had a user turn yet; the UI "
            "falls back to the chat id in that case."
        ),
    )
    last_seq: int = Field(
        default=0,
        description=(
            "Highest sequence number assigned to a chat_messages row "
            "for this chat. Authoritative cursor for cursor-replay on "
            "WS reconnect; the WS endpoint emits messages with "
            "``seq > cursor`` in order. Bumped atomically by the "
            "message writer (see primer.chat.executor)."
        ),
    )
    turn_status: Literal["idle", "claimable", "running"] = Field(
        default="idle",
        description=(
            "Lifecycle state of the FIFO queue + worker claim. "
            "``idle`` means no pending work. ``claimable`` means a "
            "user_message has landed (or an operator interrupt is "
            "pending) and a worker should pick the chat up. "
            "``running`` means a worker holds the claim and is "
            "actively processing."
        ),
    )
    cancel_requested_at: datetime | None = Field(
        default=None,
        description=(
            "Set by the API when an ``interrupt`` WS frame arrives. "
            "The owning worker polls the field via heartbeat reads "
            "AND subscribes to a ``chat:{id}:cancel`` bus event for "
            "faster wake-up. Cleared by the worker after honouring "
            "the cancellation."
        ),
    )
    pending_tool_call: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Set when the chat agent invoked a yielding tool (ask_user or an "
            "approval-gated call) and the turn ended awaiting the human's "
            "reply. Holds {tool_call_id, mode: 'ask_user'|'approval', "
            "original_call?, response_schema?}. Cleared when the reply is "
            "consumed as the pending call's tool_result. The chat surface does "
            "NOT park; this is purely in-conversation state."
        ),
    )
    pending_handoff: str | None = Field(
        default=None,
        description=(
            "Set by switch_to_agent: the prompt the NEXT turn runs with the "
            "newly-switched agent. The dispatch loop injects it as a "
            "user_message + flips claimable, then clears it. Distinct from "
            "pending_tool_call (which awaits a human reply)."
        ),
    )


class ChatMessage(Identifiable):
    """One row in the per-chat append-only message log.

    Each chat has its own monotonically-increasing ``seq`` starting
    at 1. The composite uniqueness ``(chat_id, seq)`` is the natural
    primary key; we encode it into ``id`` so the existing
    :class:`Storage[T]` interface (which is keyed on ``id``) works
    without a custom backend.

    ``id`` shape: ``"{chat_id}:{seq:020d}"`` — zero-padded seq so
    lexicographic ordering matches numeric ordering (handy for
    cursor pagination and debugging).
    """

    chat_id: str = Field(..., min_length=1)
    seq: int = Field(..., ge=1)
    kind: ChatMessageKind = Field(...)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(...)

    @staticmethod
    def make_id(chat_id: str, seq: int) -> str:
        """Compose a deterministic id from ``(chat_id, seq)``.

        Zero-padding to 20 digits is enough for any plausible chat
        length (2^63 messages per chat). Lexicographic comparison
        on the id then mirrors numeric comparison on ``seq``.
        """
        return f"{chat_id}:{seq:020d}"


__all__ = [
    "Chat",
    "ChatMessage",
    "ChatMessageKind",
    "ChatStatus",
]
