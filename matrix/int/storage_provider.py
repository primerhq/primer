"""Abstract base class for Storage providers.

A *Storage provider* owns the shared backend state (connection pool,
schema setup, prepared statements) and spawns model-bound
:class:`matrix.int.Storage` instances on demand. One application
typically constructs one provider per backend at startup, then asks
it for a :class:`Storage` whenever it needs to operate on a particular
model class:

.. code-block:: python

    provider = StorageProviderFactory.create(config)
    await provider.initialize()
    try:
        documents = provider.get_storage(Document)
        collections = provider.get_storage(Collection)
        # ... use them ...
    finally:
        await provider.aclose()

The provider, not the individual :class:`Storage`, holds the
connection pool. Multiple ``get_storage(...)`` calls return Storage
handles that share the pool. The provider caches handles by model
class so calls for the same class return the same instance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar

from matrix.int.storage import Storage
from matrix.model.common import Identifiable


ModelT = TypeVar("ModelT", bound=Identifiable)


class StorageProvider(ABC):
    """Backend-agnostic factory for model-bound :class:`Storage` handles.

    Subclasses bind to one backend (Postgres, SQLite, MongoDB, etc.)
    and one provider-specific config. ``initialize`` opens the pool /
    runs schema migrations; ``aclose`` tears it down.
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Open the connection pool and run any one-time schema setup.

        Idempotent: calling on an already-initialised provider is a
        no-op. MUST be awaited before the first :meth:`get_storage`
        call's returned handle is used.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Close the connection pool and release backend resources.

        Idempotent: calling on a never-initialised or already-closed
        provider is a no-op.
        """

    @abstractmethod
    def get_storage(self, model_class: type[ModelT]) -> Storage[ModelT]:
        """Return a :class:`Storage` handle for the given Pydantic model.

        Concrete providers map ``model_class`` to a backend table
        (typically by the lowercased class name, with collisions
        handled at registration time). The returned handle is cached
        on the provider so repeated calls for the same model yield
        the same instance, sharing the pool with sibling handles.

        The provider does NOT eagerly create the table -- backends
        defer DDL to the first write. To force eager setup, call a
        method on the returned handle (e.g. ``await
        handle.list(OffsetPage(length=1))``) inside ``initialize``.
        """
