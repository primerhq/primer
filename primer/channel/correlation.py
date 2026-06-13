"""Persistent routing store: (channel_id, anchor) -> ChannelCorrelation.

Replaces in-memory adapter correlation with a durable SQLite/Postgres-backed
store so routing survives process restarts and works across multi-process
deployments.
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.model.channel_correlation import ChannelCorrelation
from primer.model.storage import OffsetPage
from primer.storage.q import Q


ACTIVE_CHAT_ANCHOR = "__active_chat__"


class CorrelationStore:
    """CRUD wrapper around :class:`~primer.model.channel_correlation.ChannelCorrelation`.

    Parameters
    ----------
    storage_provider:
        A :class:`~primer.int.storage_provider.StorageProvider` instance
        (SQLite or Postgres) that has already been initialized.
    """

    def __init__(self, storage_provider: object) -> None:
        self._sp = storage_provider

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _storage(self):
        return self._sp.get_storage(ChannelCorrelation)

    async def lookup(self, channel_id: str, anchor: str) -> ChannelCorrelation | None:
        """Return the correlation record for *(channel_id, anchor)*, or ``None``."""
        page = await self._storage().find(
            Q(ChannelCorrelation)
            .where("channel_id", channel_id)
            .where("anchor", anchor)
            .build(),
            OffsetPage(offset=0, length=2),
        )
        return page.items[0] if page.items else None

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    async def upsert_session(
        self,
        *,
        channel_id: str,
        anchor: str,
        workspace_id: str,
        session_id: str,
        tool_call_id: str,
    ) -> ChannelCorrelation:
        """Create or update a ``kind="session"`` correlation record."""
        now = datetime.now(timezone.utc)
        existing = await self.lookup(channel_id, anchor)
        if existing is not None:
            updated = existing.model_copy(
                update={
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "tool_call_id": tool_call_id,
                    "updated_at": now,
                }
            )
            await self._storage().update(updated)
            return updated
        record = ChannelCorrelation(
            channel_id=channel_id,
            anchor=anchor,
            kind="session",
            workspace_id=workspace_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            updated_at=now,
        )
        await self._storage().create(record)
        return record

    async def upsert_chat(
        self,
        *,
        channel_id: str,
        anchor: str,
        chat_id: str,
    ) -> ChannelCorrelation:
        """Create or update a ``kind="chat"`` correlation record."""
        now = datetime.now(timezone.utc)
        existing = await self.lookup(channel_id, anchor)
        if existing is not None:
            updated = existing.model_copy(
                update={
                    "chat_id": chat_id,
                    "updated_at": now,
                }
            )
            await self._storage().update(updated)
            return updated
        record = ChannelCorrelation(
            channel_id=channel_id,
            anchor=anchor,
            kind="chat",
            chat_id=chat_id,
            updated_at=now,
        )
        await self._storage().create(record)
        return record

    async def set_active_chat(self, channel_id: str, chat_id: str) -> ChannelCorrelation:
        """Set the ``ACTIVE_CHAT_ANCHOR`` record for *channel_id* to *chat_id*."""
        return await self.upsert_chat(
            channel_id=channel_id,
            anchor=ACTIVE_CHAT_ANCHOR,
            chat_id=chat_id,
        )

    async def list_for_channel(self, channel_id: str) -> list[ChannelCorrelation]:
        """Return all correlation records for *channel_id*.

        Pages internally using a window of 200 rows until exhausted.
        """
        results: list[ChannelCorrelation] = []
        offset = 0
        while True:
            page = await self._storage().find(
                Q(ChannelCorrelation).where("channel_id", channel_id).build(),
                OffsetPage(offset=offset, length=200),
            )
            results.extend(page.items)
            if len(page.items) < 200:
                break
            offset += 200
        return results

    async def clear(self, channel_id: str, anchor: str) -> None:
        """Delete the correlation record for *(channel_id, anchor)* if it exists."""
        existing = await self.lookup(channel_id, anchor)
        if existing is not None:
            await self._storage().delete(existing.id)


__all__ = ["CorrelationStore", "ACTIVE_CHAT_ANCHOR"]
