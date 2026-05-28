"""Concrete VectorStore implementations.

Each implementation subclasses :class:`primer.int.VectorStoreProvider`
and exposes a single :class:`primer.int.VectorStore` handle that
manages per-collection vector tables. Use the factory to obtain a
provider:

.. code-block:: python

    from primer.vector import VectorStoreProviderFactory
    from primer.model.provider import VectorStoreProviderConfig

    provider = VectorStoreProviderFactory.create(config)
    await provider.initialize()
    store = provider.get_vector_store()
    await store.create_collection("kb-1", dimensions=1536)
    # ... put / search / get / delete ...
    reports = await provider.maintain_indexes()
    await provider.aclose()
"""

from primer.vector.factory import VectorStoreProviderFactory
from primer.vector.pgvector import PgVectorStore, PgVectorStoreProvider
from primer.vector.pgvectorscale import (
    PgVectorScaleStore,
    PgVectorScaleStoreProvider,
)


__all__ = [
    "PgVectorScaleStore",
    "PgVectorScaleStoreProvider",
    "PgVectorStore",
    "PgVectorStoreProvider",
    "VectorStoreProviderFactory",
]
