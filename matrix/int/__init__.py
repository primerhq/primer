"""Provider-agnostic interface ABCs.

Eleven abstract base classes are exported:

* :class:`LLM` -- streaming chat interface (text/multimodal in,
  :class:`StreamEvent` out).
* :class:`Embedder` -- embedding interface (multimodal in,
  :class:`EmbedResponse` out).
* :class:`CrossEncoder` -- cross-encoder reranker interface
  (``(query, document) -> relevance score``); used by
  :class:`matrix.search.CollectionSearcher` for two-stage rerank.
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
* :class:`Scheduler` -- distributed coordinator that decides which
  worker runs which session; enqueues, leases, and atomically
  completes turns. See
  ``docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md``.

See ``research/abc_interface.md`` for the design rationale and
per-provider adapter mapping rules.
"""

from matrix.int.cross_encoder import CrossEncoder
from matrix.int.embedder import Embedder
from matrix.int.llm import LLM
from matrix.int.scheduler import Scheduler
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
    "CrossEncoder",
    "Embedder",
    "LLM",
    "MaintenanceReport",
    "Scheduler",
    "Storage",
    "StorageProvider",
    "ToolsetProvider",
    "VectorStore",
    "VectorStoreProvider",
    "Workspace",
    "WorkspaceBackend",
]
