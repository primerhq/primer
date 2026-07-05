"""Append-user-message service helper.

Extracted from :mod:`primer.api.routers.chats` so both the WS recv loop
and the trigger dispatcher (Phase 4+) call into the same canonical
``user_message`` persist path. Spec §12.4 (Plan §3.1).

Responsibilities:

* Allocate the next ``seq`` (``chat.last_seq + 1``).
* Persist a :class:`ChatMessage` row with ``kind="user_message"`` and
  a payload of ``{"parts": ..., "content": ...}``.
* Derive ``chat.title`` from the flat user text the first time a turn
  lands (mirrors :func:`primer.chat.executor._derive_chat_title`).
* Update the chat row with the bumped ``last_seq`` (+ title).

The helper accepts either Pydantic :class:`primer.model.chat.Part`
objects (as the router does today) or raw ``dict`` parts (as the
trigger dispatcher will produce after template rendering). The
attribution dict, when supplied, is stamped onto ``payload["trigger"]``
so downstream UIs / audit trails can show which subscription fired
this user turn.

The caller is responsible for the next steps: flipping
``chat.turn_status`` to ``"claimable"`` and publishing the
``chat-claimable`` bus event so a worker picks the turn up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from primer.model.chats import Chat, ChatMessage


def _normalize_parts(parts: list) -> tuple[list[dict], list]:
    """Return (json_parts, pydantic_like_parts).

    ``json_parts`` is the list that lands in ``payload["parts"]``;
    ``pydantic_like_parts`` is the list passed to ``_derive_chat_title``
    (which inspects ``.text`` via ``getattr`` so dicts also work).
    """
    json_parts: list[dict] = []
    title_parts: list = []
    for p in parts:
        # Pydantic Part — has model_dump.
        if hasattr(p, "model_dump"):
            json_parts.append(p.model_dump(mode="json"))
            title_parts.append(p)
            continue
        # Dict-shaped part — pass through unchanged.
        if isinstance(p, dict):
            json_parts.append(p)
            # Wrap in a tiny shim so _derive_chat_title's getattr(p, "text")
            # finds the text attribute on dict-shaped parts.
            title_parts.append(_DictPartShim(p))
            continue
        # Anything else is a programming error.
        raise TypeError(
            f"append_user_message: unsupported part type {type(p).__name__!r}"
        )
    return json_parts, title_parts


class _DictPartShim:
    """Adapt a ``{"type": ..., "text": ...}`` dict to the ``.text``
    attribute shape that :func:`_derive_chat_title` expects."""

    __slots__ = ("_d",)

    def __init__(self, d: dict) -> None:
        self._d = d

    @property
    def text(self) -> Any:
        return self._d.get("text")


def _flat_text(parts_for_payload: list[dict]) -> str:
    """Join all ``type == 'text'`` parts' text fields with newlines.

    Mirrors the existing router helper's behaviour exactly — text-only
    parts are concatenated, non-text parts are skipped. Used for the
    ``payload["content"]`` field.
    """
    chunks: list[str] = []
    for p in parts_for_payload:
        if not isinstance(p, dict):
            continue
        if p.get("type") != "text":
            continue
        text = p.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks)


async def append_user_message(
    *,
    chat: Chat,
    parts: list,
    storage_provider: Any,
    attribution: dict | None = None,
    client_msg_id: str | None = None,
) -> ChatMessage:
    """Persist a ``user_message`` row, bump ``chat.last_seq``, derive title.

    Returns the persisted :class:`ChatMessage`. The caller is responsible
    for flipping ``chat.turn_status`` and publishing claim events.

    Parameters
    ----------
    chat:
        The in-memory chat row. Mutated: ``last_seq`` and (on the first
        turn) ``title`` are updated before being persisted via
        ``chats_storage.update``.
    parts:
        Either Pydantic Part objects or raw dicts. See module docstring.
    storage_provider:
        Anything with ``get_storage(model_cls)``. The helper resolves
        :class:`Chat` and :class:`ChatMessage` storages internally.
    attribution:
        Optional ``{"trigger_id": ..., "subscription_id": ...,
        "fire_id": ...}`` dict. Stamped onto ``payload["trigger"]`` so
        downstream UIs can render which trigger fired this turn. ``None``
        for human-driven turns from the WS recv loop.
    client_msg_id:
        Optional client-generated id carried on a deferred-queue
        follow-up. Stamped onto ``payload["client_msg_id"]`` so the
        client can reconcile its optimistic "(queued)" placeholder with
        the realized row when it arrives over the WS. ``None`` on the
        ordinary idle send path (the client renders the server echo
        directly and has no placeholder to reconcile).
    """
    chats_storage = storage_provider.get_storage(Chat)
    messages_storage = storage_provider.get_storage(ChatMessage)

    json_parts, title_parts = _normalize_parts(parts)
    flat = _flat_text(json_parts)

    payload: dict[str, Any] = {"parts": json_parts}
    if flat:
        payload["content"] = flat
    if attribution:
        payload["trigger"] = dict(attribution)
    if client_msg_id:
        payload["client_msg_id"] = client_msg_id

    next_seq = chat.last_seq + 1
    row = ChatMessage(
        id=ChatMessage.make_id(chat.id, next_seq),
        chat_id=chat.id,
        seq=next_seq,
        kind="user_message",
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )
    await messages_storage.create(row)

    if chat.title is None:
        # Local import: executor pulls in heavy chat-runtime imports that
        # we don't want at module import time.
        from primer.chat.executor import _derive_chat_title

        chat.title = _derive_chat_title(title_parts)
    chat.last_seq = next_seq
    await chats_storage.update(chat)
    return row


__all__ = ["append_user_message"]
