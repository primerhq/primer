"""Migration 19: drop ``relay_mode`` from stored channel rows.

``Channel.config.chats.relay_mode`` (``final`` or ``all``, "outbound
verbosity") was stored and never read: how much a conversation hears is
the channel trigger's ``interactive`` flag. It was removed from
:class:`~primer.model.channel.ChatConfig` (ticket 01a16e37-b98e, item 7),
and that model forbids extras, so a channel row that still carries the
key is unreadable until this step removes it, the way ``m003`` removed
the retired agent fields from the same block.

Get-then-write, and a row with no ``relay_mode`` is left alone, so
re-running converges. The view declared here addresses the same
``channel`` table (the storage layer derives the table name from
``model_class.__name__.lower()``), and ``extra="allow"`` carries every
other field through the read-modify-write untouched; ``config`` is read
as a plain dict, so a row the live model refuses is read as it is.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict, Field

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

_PAGE = 200

#: The key this step removes from ``config.chats``.
_RETIRED_KEY = "relay_mode"


class Channel(Identifiable):  # noqa: N801 - name selects the storage table
    """Migration-local view of a channel row, with an untyped config block.

    ``config`` may be ``None``: a row stored before the live model's default was written (or by a tool that omits it) carries
    ``config: null``, and the step must read such a row to keep paging, leaving it alone (round 2 of #932, item 7).
    """

    model_config = ConfigDict(extra="allow")

    config: dict[str, Any] | None = Field(default_factory=dict)


class M019DropChatRelayMode:
    """Remove the retired ``relay_mode`` key from every stored channel's ``config.chats``."""

    version = 19
    description = "drop the retired Channel.config.chats.relay_mode key"

    async def apply(self, sp: StorageProvider) -> None:
        channels = sp.get_storage(Channel)
        stripped = 0
        offset = 0
        rows: list[Channel] = []
        # Read every row before writing any: an update can move a row within the list order, and paging while writing could
        # skip one.
        while True:
            page = await channels.list(OffsetPage(offset=offset, length=_PAGE))
            rows.extend(page.items)
            if len(page.items) < _PAGE:
                break
            offset += _PAGE
        for row in rows:
            chats = (row.config or {}).get("chats")
            if not isinstance(chats, dict) or _RETIRED_KEY not in chats:
                continue  # nothing to drop, or already migrated
            cleaned = {key: value for key, value in chats.items() if key != _RETIRED_KEY}
            await channels.update(row.model_copy(update={"config": {**row.config, "chats": cleaned}}))
            stripped += 1
        if stripped:
            logger.info("dropped the retired chats.relay_mode key from channel rows", extra={"count": stripped})


__all__ = ["M019DropChatRelayMode"]
