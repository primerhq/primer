"""An agent's memory collection: provisioning, deletion, and the access it answers to (agent memory Task 2).

Entries are documents in the collection ``mem-<agent id>``. The collection carries ``memory_of`` and no access block of its own: it answers to the
agent's (:func:`memory_access`), so anyone who can read the agent reads its entries, and every generic write door refuses it
(:data:`MEMORY_READ_ONLY_CODE`); only ``primer/memory/`` writes it. It is left out of every collection listing.
"""

from __future__ import annotations

import logging
from typing import Any

from primer.knowledge.lifecycle import enable_search, purge_collection
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.collection import MEMORY_COLLECTION_PREFIX, Collection, CollectionEmbedder, CollectionSearchConfig
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.internal import INTERNAL_COLLECTIONS_CONFIG_ID, InternalCollectionsConfig
from primer.model.memory import AgentMemory

logger = logging.getLogger(__name__)

MEMORY_PREFIX = MEMORY_COLLECTION_PREFIX
#: The code every generic write door answers with for a memory collection.
MEMORY_READ_ONLY_CODE = "memory_collection_read_only"


def memory_read_only_message(collection_id: str) -> str:
    return (
        f"collection {collection_id!r} is an agent's memory and is read-only through this door; its entries are written only by the agent's "
        "memory (the memory routes and the background worker)"
    )


def memory_collection_id(agent_id: str) -> str:
    return f"{MEMORY_PREFIX}{agent_id}"


async def memory_access(sp: Any, collection: Collection) -> Access:
    """The agent's ``access`` for a collection with ``memory_of``; nobody's (an empty block) when the agent is gone. ``sp`` is a storage provider or
    anything with ``get_storage``; a bare ``Storage[Agent]`` is accepted too."""
    agents = sp.get_storage(Agent) if hasattr(sp, "get_storage") else sp
    agent = await agents.get(collection.memory_of) if collection.memory_of else None
    return agent.access if agent is not None else Access()


def _search_key(search: CollectionSearchConfig | None) -> dict | None:
    return None if search is None else search.model_dump(mode="json", exclude={"state", "error"})


async def _install_default(sp: Any) -> CollectionSearchConfig | None:
    """The search the install's internal collections use, or None when that subsystem is not configured."""
    cfg = await sp.get_storage(InternalCollectionsConfig).get(INTERNAL_COLLECTIONS_CONFIG_ID)
    if cfg is None:
        return None
    return CollectionSearchConfig(
        embedder=CollectionEmbedder(provider_id=cfg.embedding_provider_id, model=cfg.embedding_model),
        vector_store_provider_id=cfg.search_provider_id,
        cross_encoder=cfg.cross_encoder,
    )


async def ensure_memory_collection(sp: Any, provider_registry: Any, semantic_search_registry: Any, agent: Agent, settings: AgentMemory) -> Collection:
    """The agent's memory collection, created if missing and given search when its settings (else the install default) name one. Idempotent: an
    existing collection whose search already matches is left alone. An explicit ``settings.search`` the providers cannot serve raises; the install
    default failing leaves the collection on full-text search alone (logged)."""
    colls = sp.get_storage(Collection)
    cid = memory_collection_id(agent.id)
    row = await colls.get(cid)
    if row is None:
        try:
            row = await colls.create(Collection(id=cid, description=f"Memory of agent {agent.id}", memory_of=agent.id))
        except ConflictError:
            row = await colls.get(cid)
            if row is None:
                raise
    wanted = settings.search
    explicit = wanted is not None
    if wanted is None:
        wanted = await _install_default(sp)
    if wanted is None or _search_key(row.search) == _search_key(wanted):
        return row
    try:
        return await enable_search(sp, provider_registry, semantic_search_registry, collection_id=cid, cfg=wanted)
    except ConflictError:
        if explicit:
            raise
        logger.warning("agent %s: the install's default search providers cannot serve its memory; entries are searched by full text", agent.id)
        return row


async def purge_memory_entries(sp: Any, agent_id: str, *, semantic_search_registry: Any = None) -> int:
    """Remove every entry (documents, content rows, and the vectors when the registry is given) and keep the collection. Returns how many
    documents went; 0 when there is none."""
    row = await sp.get_storage(Collection).get(memory_collection_id(agent_id))
    if row is None:
        return 0
    return await purge_collection(sp, semantic_search_registry, collection=row)


async def delete_memory_collection(sp: Any, semantic_search_registry: Any, agent_id: str) -> None:
    """Remove the agent's memory collection with everything it holds. Idempotent."""
    colls = sp.get_storage(Collection)
    row = await colls.get(memory_collection_id(agent_id))
    if row is None:
        return
    await purge_collection(sp, semantic_search_registry, collection=row)
    try:
        await colls.delete(row.id)
    except NotFoundError:
        pass


__all__ = [
    "MEMORY_PREFIX", "MEMORY_READ_ONLY_CODE", "delete_memory_collection", "ensure_memory_collection", "memory_access", "memory_collection_id",
    "memory_read_only_message", "purge_memory_entries",
]
