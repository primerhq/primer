"""Migration 26: existing model profiles and providers become public (access-control Phase 4, Task 3, spec 8).

Model profiles and the providers a user-owned entity references now carry a use grant (``Grantable``). A new row is private, but every row
that existed before access control stays usable by everyone: a row whose ``access`` block is absent or empty (no owners, no grants, not
public) becomes public (``members`` carries the ``public`` token, frozen with this step). A row that already carries grants or ``public``
is left alone, so re-running converges and an admin's later choice is never undone.

Seven tables, through migration-local views with ``extra="allow"``, read in full before any write, as ``m021`` does: model profiles, LLM,
embedding, cross-encoder, semantic-search (vector store), workspace and channel providers.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

_PAGE = 200


class _View(Identifiable):
    model_config = ConfigDict(extra="allow")

    access: dict[str, Any] | None = None


# The class name selects the table (the storage layer lowercases it).
class ModelProfile(_View):  # noqa: N801
    pass


class LLMProvider(_View):  # noqa: N801
    pass


class EmbeddingProvider(_View):  # noqa: N801
    pass


class CrossEncoderProvider(_View):  # noqa: N801
    pass


class SemanticSearchProvider(_View):  # noqa: N801
    pass


class WorkspaceProvider(_View):  # noqa: N801
    pass


class ChannelProvider(_View):  # noqa: N801
    pass


_VIEWS = (ModelProfile, LLMProvider, EmbeddingProvider, CrossEncoderProvider, SemanticSearchProvider, WorkspaceProvider, ChannelProvider)

_PUBLIC: dict[str, Any] = {"owners": [], "grants": {}, "public": True, "public_tools": [], "members": ["public"], "rev": 0}


def _unset(access: dict[str, Any] | None) -> bool:
    return not access or not (access.get("owners") or access.get("grants") or access.get("public"))


async def _read_all(store) -> list[Any]:
    rows: list[Any] = []
    offset = 0
    while True:
        page = await store.list(OffsetPage(offset=offset, length=_PAGE))
        rows.extend(page.items)
        if len(page.items) < _PAGE:
            return rows
        offset += _PAGE


class M026InfrastructurePublic:
    """Make every existing profile and provider with no sharing public."""

    version = 26
    description = "existing model profiles and providers become public"

    async def apply(self, sp: StorageProvider) -> None:
        total = 0
        for view in _VIEWS:
            store = sp.get_storage(view)
            for row in await _read_all(store):
                if not _unset(row.access):
                    continue
                await store.update(row.model_copy(update={"access": dict(_PUBLIC)}))
                total += 1
        if total:
            logger.info("made existing infrastructure public", extra={"rows": total})


__all__ = ["M026InfrastructurePublic"]
