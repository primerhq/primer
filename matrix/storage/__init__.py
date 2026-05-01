"""Concrete Storage implementations.

Each implementation subclasses :class:`matrix.int.StorageProvider` and
exposes model-bound :class:`matrix.int.Storage` handles via
``get_storage(model_class)``. Use the factory to obtain a provider:

.. code-block:: python

    from matrix.storage import StorageProviderFactory
    from matrix.model.provider import StorageProviderConfig

    provider = StorageProviderFactory.create(config)
    await provider.initialize()
    documents = provider.get_storage(Document)
    # ...
    await provider.aclose()
"""

from matrix.storage.factory import StorageProviderFactory
from matrix.storage.postgres import PostgresStorage, PostgresStorageProvider


__all__ = [
    "PostgresStorage",
    "PostgresStorageProvider",
    "StorageProviderFactory",
]
