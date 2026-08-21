"""Migration 3: make pre-cutover Channel and Collection rows readable again.

The session cutover changed the shape of two stored entities without
moving the rows that were already written, so an install upgraded across
it fails to load them:

* ``Channel.config.chats`` carried ``default_agent``, ``allowed_agents``
  and ``allow_agent_switch`` when a channel chose its own agent. Bindings
  replaced that, and :class:`~primer.model.channel.ChatConfig` forbids
  extras, so every legacy channel row raises ``extra_forbidden`` and the
  startup warm pass gives up on all of them.
* ``Collection`` kept ``embedder`` and ``search_provider_id`` at the top
  level and used ``search`` for the rerank toggles alone. Those two moved
  *into* ``search`` as ``embedder`` and ``vector_store_provider_id``,
  both required, so legacy rows raise ``missing`` and the collection is
  unreadable -- including the system collection, which fails the seed.

Both steps are get-then-write and skip rows that already carry the new
shape, so re-running converges.

**Why this module redefines the two models.** A migration reads rows
written by the old schema from inside a build where the models have
already changed, so loading them through the live classes is exactly the
failure being repaired. The storage layer derives the table name from
``model_class.__name__.lower()``, so the views declared here address the
same ``channel`` and ``collection`` tables, and ``extra="allow"`` carries
every field this migration does not care about through the
read-modify-write untouched.

``mmr`` has no counterpart on the new search block and is dropped with
the rest of the legacy ``search`` contents. ``state`` is stamped
``"ready"`` rather than ``"indexing"``: the vectors these rows point at
were built by the old build and are still in the store, so ``"indexing"``
would claim an index pass is in flight that nothing is going to finish.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

_PAGE = 200

#: Fields that lived on ``config.chats`` while a channel picked its own
#: agent. Bindings own that choice now and ``ChatConfig`` forbids extras.
_RETIRED_CHAT_FIELDS = ("default_agent", "allowed_agents", "allow_agent_switch")


class Channel(Identifiable):  # noqa: N801 - name selects the storage table
    """Migration-local view of a channel row, with an untyped config block."""

    model_config = ConfigDict(extra="allow")

    config: dict[str, Any] = Field(default_factory=dict)


class Collection(Identifiable):  # noqa: N801 - name selects the storage table
    """Migration-local view of a collection row, pre- and post-move.

    ``embedder`` and ``search_provider_id`` are the legacy top-level
    fields. The live model ignores unknown keys rather than rejecting
    them, so they may be left behind; they are cleared anyway to keep a
    migrated row from reading as though it still had two sources of
    truth.
    """

    model_config = ConfigDict(extra="allow")

    embedder: dict[str, Any] | None = Field(default=None)
    search_provider_id: str | None = Field(default=None)
    search: dict[str, Any] | None = Field(default=None)


async def _iter_rows(storage):
    """Page through every row of one table."""
    offset = 0
    while True:
        page = await storage.list(OffsetPage(offset=offset, length=_PAGE))
        if not page.items:
            return
        for row in page.items:
            yield row
        if len(page.items) < _PAGE:
            return
        offset += _PAGE


class M003SessionCutover:
    """Carry channel and collection rows across the session cutover."""

    version = 3
    description = "drop retired Channel.chats agent fields; nest Collection search"

    async def apply(self, sp: StorageProvider) -> None:
        await self._strip_channel_agent_fields(sp)
        await self._nest_collection_search(sp)

    # -- step 1 ----------------------------------------------------------
    async def _strip_channel_agent_fields(self, sp: StorageProvider) -> None:
        channels = sp.get_storage(Channel)
        stripped = 0
        async for row in _iter_rows(channels):
            chats = (row.config or {}).get("chats")
            if not isinstance(chats, dict):
                continue
            if not any(key in chats for key in _RETIRED_CHAT_FIELDS):
                continue  # already migrated
            cleaned = {k: v for k, v in chats.items() if k not in _RETIRED_CHAT_FIELDS}
            config = {**row.config, "chats": cleaned}
            await channels.update(row.model_copy(update={"config": config}))
            stripped += 1
        if stripped:
            logger.info("stripped retired channel agent fields", extra={"count": stripped})

    # -- step 2 ----------------------------------------------------------
    async def _nest_collection_search(self, sp: StorageProvider) -> None:
        collections = sp.get_storage(Collection)
        moved = 0
        for_grep = 0
        async for row in _iter_rows(collections):
            search = row.search
            if isinstance(search, dict) and "embedder" in search:
                continue  # already migrated
            if not row.embedder or not row.search_provider_id:
                # Nothing to move. The row predates the split or was
                # written without a vector store, so grep-only is the
                # honest reading -- do not invent an embedder.
                if row.embedder or row.search_provider_id or search is not None:
                    logger.warning(
                        "collection has no complete legacy search config; "
                        "leaving it grep-only",
                        extra={"collection_id": row.id},
                    )
                    for_grep += 1
                    await collections.update(
                        row.model_copy(
                            update={
                                "search": None,
                                "embedder": None,
                                "search_provider_id": None,
                            }
                        )
                    )
                continue
            legacy = search if isinstance(search, dict) else {}
            nested: dict[str, Any] = {
                "embedder": row.embedder,
                "vector_store_provider_id": row.search_provider_id,
                "state": "ready",
                "error": None,
            }
            if isinstance(legacy.get("cer"), dict):
                nested["cross_encoder"] = legacy["cer"]
            await collections.update(
                row.model_copy(
                    update={
                        "search": nested,
                        "embedder": None,
                        "search_provider_id": None,
                    }
                )
            )
            moved += 1
        if moved:
            logger.info("nested collection search config", extra={"count": moved})
        if for_grep:
            logger.info("left collections grep-only", extra={"count": for_grep})


__all__ = ["M003SessionCutover"]
