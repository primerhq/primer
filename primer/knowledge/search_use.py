"""Who may USE a collection's search providers (access-control P4 Task 6, plan Decision 5).

A collection's search names an embedding provider, a vector store (semantic search) provider and, optionally, a cross-encoder provider. The
owner enabling search must be able to use all of them, and so must every person who searches. A provider that exists but that the person holds no
use grant on (and that is not public) is unusable; admins use any. Indexing writes by full users are not a use of their own: the owner's check at enable
time covers them (lead answer 4).
"""

from __future__ import annotations

from typing import Any

from primer.access.person import Person
from primer.access.policy import level_of
from primer.model.collection import Collection, CollectionSearchConfig
from primer.model.providers.cross_encoder import CrossEncoderProvider
from primer.model.providers.embedding import EmbeddingProvider
from primer.model.providers.vector import SemanticSearchProvider
from primer.model.user import User


def _providers(cfg: CollectionSearchConfig) -> list[tuple[type, str]]:
    refs: list[tuple[type, str]] = [
        (EmbeddingProvider, cfg.embedder.provider_id),
        (SemanticSearchProvider, cfg.vector_store_provider_id),
    ]
    if cfg.cross_encoder is not None:
        refs.append((CrossEncoderProvider, cfg.cross_encoder.provider_id))
    return refs


async def unusable_search_provider(storage_provider: Any, person: Person, cfg: CollectionSearchConfig) -> tuple[type, str] | None:
    """The first provider of ``cfg`` (model class and id) that exists but that ``person`` may not use, else ``None``. A missing provider is not
    this check's: the existing not-registered answers cover it."""
    for model, provider_id in _providers(cfg):
        row = await storage_provider.get_storage(model).get(provider_id)
        if row is not None and level_of(person, row.access) is None:
            return model, provider_id
    return None


async def owners_may_use(storage_provider: Any, collection: Collection) -> bool:
    """Whether some owner of ``collection`` may use every provider its search names. Indexing spends them on the owners' behalf, so it
    asks this as a defence behind the enable check (security review of P4, finding 2). A collection with no owner (a platform or admin
    row) passes."""
    if collection.search is None or not collection.access.owners:
        return True
    users = storage_provider.get_storage(User)
    for user_id in collection.access.owners:
        user = await users.get(user_id)
        if user is not None and await unusable_search_provider(storage_provider, Person(user_id, user.role), collection.search) is None:
            return True
    return False


__all__ = ["owners_may_use", "unusable_search_provider"]
