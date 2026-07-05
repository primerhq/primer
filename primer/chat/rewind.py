"""Rewind-truncation helper (Task A7, chat-refactor plan, spec R4).

Truncating a chat's history back to a chosen ``user_message`` row lets
an operator discard everything after a bad turn and retry — the
"rewind" affordance on the composer. Extracted into its own module
(mirrors :mod:`primer.chat.enqueue`) so the REST endpoint
(:mod:`primer.api.routers.chats`) and any future WS / CLI entry point
share one canonical truncation path.
"""

from __future__ import annotations

from typing import Any

from primer.model.chats import Chat, ChatMessage
from primer.model.except_ import NotFoundError
from primer.model.storage import CursorPage, Op, OrderBy
from primer.storage.q import Q


async def truncate_chat_after(
    chat: Chat,
    target_seq: int,
    *,
    storage_provider: Any,
) -> int:
    """Delete every ``ChatMessage`` row with ``seq > target_seq``.

    Keeps the row at ``target_seq`` (and everything before it) — the
    caller (the REST endpoint) has already validated that row exists
    and is a legal rewind target. Mutates and persists ``chat``:

    * ``last_seq`` resets to ``target_seq``.
    * ``next_unprocessed_seq`` clamps to
      ``min(next_unprocessed_seq, target_seq)`` so the worker-side
      claim drain re-scans the kept tail rather than skipping over it.
    * ``pending_tool_call`` / ``pending_handoff`` / ``cancel_requested_at``
      are cleared — any in-conversation gate or cancel flag may have
      referred to rows that no longer exist after the truncation.

    Returns the number of rows deleted. Drains via the same paged
    delete loop :func:`primer.api.routers.chats.end_chat` uses with
    ``force=True`` (a ``CursorPage`` capped at 200 rows/page) so an
    arbitrarily long discarded tail never loads into memory at once.

    The caller is responsible for all pre-flight guards (404 / 409 /
    422) and for publishing the ``chat:{id}:tick`` bus event afterward
    — this helper only performs the storage mutation.
    """
    messages_storage = storage_provider.get_storage(ChatMessage)
    chats_storage = storage_provider.get_storage(Chat)

    deleted = 0
    cursor: str | None = None
    predicate = (
        Q(ChatMessage)
        .where("chat_id", chat.id)
        .where_op("seq", Op.GT, target_seq)
        .build()
    )
    while True:
        page = await messages_storage.find(
            predicate,
            CursorPage(cursor=cursor, length=200),
            order_by=[OrderBy(field="seq", direction="asc")],
        )
        for row in page.items:
            try:
                await messages_storage.delete(row.id)
                deleted += 1
            except NotFoundError:
                pass
        cursor = getattr(page, "next_cursor", None)
        if not cursor:
            break

    chat.last_seq = target_seq
    chat.next_unprocessed_seq = min(chat.next_unprocessed_seq, target_seq)
    chat.pending_tool_call = None
    chat.pending_handoff = None
    chat.cancel_requested_at = None
    await chats_storage.update(chat)

    return deleted


__all__ = ["truncate_chat_after"]
