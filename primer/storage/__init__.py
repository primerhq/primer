"""Concrete Storage implementations.

Each implementation subclasses :class:`primer.int.StorageProvider` and
exposes model-bound :class:`primer.int.Storage` handles via
``get_storage(model_class)``. Use the factory to obtain a provider:

.. code-block:: python

    from primer.storage import StorageProviderFactory
    from primer.model.provider import StorageProviderConfig

    provider = StorageProviderFactory.create(config)
    await provider.initialize()
    documents = provider.get_storage(Document)
    # ...
    await provider.aclose()
"""

from primer.storage.factory import StorageProviderFactory
from primer.storage.postgres import PostgresStorage, PostgresStorageProvider
from primer.storage.sqlite import SqliteStorage, SqliteStorageProvider


__all__ = [
    "PostgresStorage",
    "PostgresStorageProvider",
    "SqliteStorage",
    "SqliteStorageProvider",
    "StorageProviderFactory",
]
