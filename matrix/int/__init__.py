"""Provider-agnostic interface ABCs.

Nine abstract base classes are exported:

* :class:`LLM` -- streaming chat interface (text/multimodal in,
  :class:`StreamEvent` out).
* :class:`Embedder` -- embedding interface (multimodal in,
  :class:`EmbedResponse` out).
* :class:`ToolsetProvider` -- toolset interface (list and invoke tools).
* :class:`Storage` -- generic CRUD + search interface for any
  :class:`matrix.model.common.Identifiable` model.
* :class:`StorageProvider` -- shared backend state + factory for
  model-bound :class:`Storage` handles.
* :class:`VectorStore` -- embedding-vector store with create_collection /
  put / search / get / delete operations keyed by
  ``(collection_id, document_id, chunk_id)``.
* :class:`VectorStoreProvider` -- shared vector-DB state + factory
  for the :class:`VectorStore` handle, plus index maintenance.
* :class:`Workspace` -- one materialised sandbox + ``.state`` + ``.tmp``
  + a session registry; hosts one or more :class:`AgentSession`
  executions concurrently.
* :class:`WorkspaceBackend` -- backend-agnostic factory + lifecycle
  for :class:`Workspace` instances.

See ``research/abc_interface.md`` for the design rationale and
per-provider adapter mapping rules.
"""

from matrix.int.embedder import Embedder
from matrix.int.llm import LLM
from matrix.int.storage import Storage
from matrix.int.storage_provider import StorageProvider
from matrix.int.toolset import ToolsetProvider
from matrix.int.vector_store import VectorStore
from matrix.int.vector_store_provider import (
    MaintenanceReport,
    VectorStoreProvider,
)
from matrix.int.workspace import Workspace, WorkspaceBackend


__all__ = [
    "Embedder",
    "LLM",
    "MaintenanceReport",
    "Storage",
    "StorageProvider",
    "ToolsetProvider",
    "VectorStore",
    "VectorStoreProvider",
    "Workspace",
    "WorkspaceBackend",
]
