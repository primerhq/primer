"""Stored rows for the test toolsets a test hands the registry directly.

``ProviderRegistry.get_toolset`` serves a stored row or a built-in, so in production a toolset that is neither does not exist, and
``toolset_use`` answers "no tools" for it (a deleted toolset must never read as built-in and open to everyone). A test that injects a provider
under its own id (``_Registry(pr, {"rec": recorder})``) skips the row; this gives it a public one so the person's inner-tool access check passes.
"""

from __future__ import annotations

from typing import Any

from primer.model.access import Access
from primer.model.except_ import ConflictError
from primer.model.provider import Toolset, ToolsetProviderType


async def seed_public_toolset_rows(storage_provider: Any, *toolset_ids: str) -> None:
    store = storage_provider.get_storage(Toolset)
    for toolset_id in toolset_ids:
        try:
            await store.create(Toolset(id=toolset_id, provider=ToolsetProviderType.INTERNAL, access=Access(public=True)))
        except ConflictError:
            pass
