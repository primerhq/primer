"""Typed access to the three tables of an MCP toolset's shared sign-in, with the sealing done here (plan 07, Task 5; design 12.5 and 12.6).

:class:`McpOAuthStore` is the only code that reads or writes :class:`~primer.model.mcp_oauth.McpOAuthTokenSet`, :class:`~primer.model.mcp_oauth.McpOAuthClient`
and :class:`~primer.model.mcp_oauth.McpOAuthPendingSignIn`. It owns three guarantees the rest of the OAuth code leans on:

* **Ciphertext only at rest.** The store seals with :meth:`seal` and opens with :meth:`unseal`; a row it is handed holds ``SecretStr`` ciphertext, and what reaches
  storage is that ciphertext. A value that cannot be opened (another key, a damaged row) raises :class:`~primer.common.secret_box.SecretBoxError` at ``unseal``.
* **The token row moves by compare-and-set.** :meth:`patch_tokens` is a guarded field patch (``Storage.patch_if`` through the drift tripwire), and
  :meth:`replace_tokens` (a connect) is a compare-and-set on the version too, so two connects racing, or a connect racing a refresh, never write one version
  twice or lose an update silently. This is what lets several processes share one refresh lease.
* **A pending sign-in is single use.** :meth:`take_pending` claims the row with a compare-and-set on ``consumed`` before it returns it, so of any number of
  concurrent callers exactly one gets it, and an expired or already-claimed row is never given out.

The store logs nothing that comes from a row, and no method returns or raises with a token, a secret, a verifier or a ``state`` value in its text.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr
from pydantic_core import to_jsonable_python

from primer.common.secret_box import SecretBox, is_sealed
from primer.int.storage_provider import StorageProvider
from primer.model.common import dump_for_storage
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.mcp_oauth import McpOAuthClient, McpOAuthPendingSignIn, McpOAuthTokenSet
from primer.model.storage import CursorPage
from primer.storage import raw_generation
from primer.storage.cas import patch_if_checked
from primer.storage.q import Q

__all__ = ["McpOAuthStore"]

logger = logging.getLogger(__name__)

#: The fields that hold a sealed value. A write that carries plaintext in one of them is a bug that would put a token in the database, so the store refuses it.
_SEALED_FIELDS = ("access_token_enc", "refresh_token_enc", "client_secret_enc", "code_verifier_enc")

#: How often a connect re-reads and tries again after losing the compare-and-set to another writer. Each loss means someone else just wrote the row, so a handful of tries is generous.
_REPLACE_ATTEMPTS = 8
_PAGE = 100


def _json_ready(value: Any) -> Any:
    """``value`` as the storage patch wants it: a sealed value as its ciphertext string, a timestamp in the model's own JSON form (UTC), anything else as it is."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, datetime):
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return to_jsonable_python(value)
    return value


def _require_sealed(values: Mapping[str, Any]) -> None:
    """Refuse a write whose secret fields hold anything but a sealed value. The message names the field, never the value."""
    for name in _SEALED_FIELDS:
        value = values.get(name)
        if value is None:
            continue
        text = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not is_sealed(text):
            raise ValueError(f"{name} must hold a sealed value (McpOAuthStore.seal), never plaintext")


def _require_sealed_row(row: Any) -> None:
    _require_sealed({name: getattr(row, name, None) for name in _SEALED_FIELDS})


class McpOAuthStore:
    """Sealing plus typed access to the token, client and pending-sign-in tables."""

    def __init__(self, storage_provider: StorageProvider, box: SecretBox) -> None:
        self._box = box
        self._tokens = storage_provider.get_storage(McpOAuthTokenSet)
        self._clients = storage_provider.get_storage(McpOAuthClient)
        self._pending = storage_provider.get_storage(McpOAuthPendingSignIn)

    # ---- sealing ------------------------------------------------------------------------------------------------------------------------------------------------------

    def seal(self, plaintext: str) -> SecretStr:
        """``plaintext`` as the ciphertext a row stores, held in a ``SecretStr`` so it never prints."""
        return SecretStr(self._box.encrypt(plaintext))

    def unseal(self, sealed: SecretStr | None) -> str | None:
        """The plaintext of a sealed value, or ``None`` for ``None``. Raises :class:`~primer.common.secret_box.SecretBoxError` when it cannot be opened."""
        if sealed is None:
            return None
        return self._box.decrypt(sealed.get_secret_value())

    # ---- the token row ------------------------------------------------------------------------------------------------------------------------------------------------

    async def get_tokens(self, toolset_id: str) -> McpOAuthTokenSet | None:
        return await self._tokens.get(toolset_id)

    async def replace_tokens(self, row: McpOAuthTokenSet) -> McpOAuthTokenSet:
        """Store ``row`` as the toolset's tokens: created when there is none, else it REPLACES the stored row with ``version = stored.version + 1``.

        A connect supersedes whatever was there, a held refresh lease included (the new row carries none), and the bumped version makes any refresh that was
        in flight see that its row changed. The write is a compare-and-set on the version it read: when another writer got in between, this reads again, so two
        connects never write one version twice. A create that loses a race to another create retries as a replace.
        """
        _require_sealed_row(row)
        for _ in range(_REPLACE_ATTEMPTS):
            stored = await self._tokens.get(row.id)
            if stored is None:
                try:
                    return await self._tokens.create(row)
                except ConflictError:
                    continue
            bumped = row.model_copy(update={"version": stored.version + 1})
            document = {key: value for key, value in dump_for_storage(bumped).items() if key != "id"}
            try:
                replaced = await patch_if_checked(self._tokens, row.id, document, where={"version": [raw_generation(stored, "version")]})
            except NotFoundError:
                continue                                # deleted between the read and the write: create it
            if replaced is not None:
                return replaced
        raise ConflictError(f"the sign-in of MCP toolset {row.id} was changed by other writers {_REPLACE_ATTEMPTS} times in a row")

    async def patch_tokens(self, toolset_id: str, patch: Mapping[str, Any], *, where: Mapping[str, Sequence[Any]]) -> McpOAuthTokenSet | None:
        """Write only the named fields of the token row, iff every ``where`` clause matches its CURRENT document (``Storage.patch_if`` through the drift tripwire).

        ``patch`` takes the model's own types: a ``SecretStr`` is written as its ciphertext, a ``datetime`` as the ISO form the model stores, ``None`` as null.
        ``where`` values come from :func:`primer.storage.raw_generation` of the row that was read, never from a re-formatted value.

        Returns the stored row after the write, or ``None`` when it was not applied: the guard no longer matches (another writer got there first) or the row is
        gone (the toolset was disconnected). The caller reads again to tell which.
        """
        _require_sealed(patch)
        document = {key: _json_ready(value) for key, value in patch.items()}
        guard = {key: [_json_ready(value) for value in allowed] for key, allowed in where.items()}
        try:
            return await patch_if_checked(self._tokens, toolset_id, document, where=guard)
        except NotFoundError:
            return None

    async def delete_tokens(self, toolset_id: str) -> bool:
        """Remove the toolset's token row. ``True`` when there was one; deleting nothing is not an error."""
        try:
            await self._tokens.delete(toolset_id)
        except NotFoundError:
            return False
        return True

    # ---- the registered client ----------------------------------------------------------------------------------------------------------------------------------------

    async def get_client(self, toolset_id: str) -> McpOAuthClient | None:
        return await self._clients.get(toolset_id)

    async def put_client(self, row: McpOAuthClient) -> McpOAuthClient:
        """Store the toolset's registered client, replacing a previous registration."""
        _require_sealed_row(row)
        for _ in range(_REPLACE_ATTEMPTS):
            try:
                return await self._clients.create(row)
            except ConflictError:
                pass
            try:
                return await self._clients.update(row)
            except NotFoundError:                       # deleted in between: the registration is simply new
                continue
        raise ConflictError(f"the registered client of MCP toolset {row.id} was changed by other writers {_REPLACE_ATTEMPTS} times in a row")

    # ---- pending sign-ins ---------------------------------------------------------------------------------------------------------------------------------------------

    async def put_pending(self, row: McpOAuthPendingSignIn) -> None:
        _require_sealed_row(row)
        await self._pending.create(row)

    async def take_pending(self, state: str, *, now: datetime) -> McpOAuthPendingSignIn | None:
        """Claim the pending sign-in for ``state``: the row, once. ``None`` when it is unknown, already claimed, or expired (an expired row is deleted).

        The claim is a compare-and-set of ``consumed`` from False to True, so concurrent callers (the same callback hitting two pods, a replay) get the row
        exactly once. The row is deleted after the claim; one a crash left behind is marked consumed and cannot be claimed again.
        """
        row = await self._pending.get(state)
        if row is None or row.consumed:
            return None
        if row.expires_at <= now:
            await self._drop_pending(state)
            return None
        try:
            claimed = await patch_if_checked(self._pending, state, {"consumed": True}, where={"consumed": [False]})
        except NotFoundError:
            return None                                 # another caller claimed it AND deleted it between our read and our claim
        if claimed is None:
            return None
        await self._drop_pending(state)
        return claimed

    async def purge_pending(self, toolset_id: str, *, now: datetime) -> int:
        """Delete the toolset's expired pending sign-ins; the number deleted. Live ones are left alone."""
        removed = 0
        for row in await self._pending_of(toolset_id):
            if row.expires_at <= now and await self._drop_pending(row.id):
                removed += 1
        return removed

    async def forget(self, toolset_id: str) -> None:
        """Remove everything stored for the toolset: its tokens, its registered client and every pending sign-in, live ones included."""
        await self.delete_tokens(toolset_id)
        try:
            await self._clients.delete(toolset_id)
        except NotFoundError:
            pass
        for row in await self._pending_of(toolset_id):
            await self._drop_pending(row.id)

    async def _pending_of(self, toolset_id: str) -> list[McpOAuthPendingSignIn]:
        predicate = Q(McpOAuthPendingSignIn).where("toolset_id", toolset_id).build()
        rows: list[McpOAuthPendingSignIn] = []
        cursor: str | None = None
        while True:
            page = await self._pending.find(predicate, CursorPage(cursor=cursor, length=_PAGE))
            rows.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return rows

    async def _drop_pending(self, state: str) -> bool:
        try:
            await self._pending.delete(state)
        except NotFoundError:
            return False
        return True
