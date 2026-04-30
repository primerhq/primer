"""Provider-agnostic interface ABCs.

Five abstract base classes are exported:

* :class:`LLM` -- streaming chat interface (text/multimodal in,
  :class:`StreamEvent` out).
* :class:`Embedder` -- embedding interface (multimodal in,
  :class:`EmbedResponse` out).
* :class:`ToolsetProvider` -- toolset interface (list and invoke tools).
* :class:`Storage` -- generic CRUD + search interface for any
  :class:`matrix.model.common.Identifiable` model.
* :class:`VectorStore` -- embedding-vector store with put / search /
  get / delete operations keyed by
  ``(collection_id, document_id, chunk_id)``.

See ``research/abc_interface.md`` for the design rationale and
per-provider adapter mapping rules.
"""

from matrix.int.embedder import Embedder
from matrix.int.llm import LLM
from matrix.int.storage import Storage
from matrix.int.toolset import ToolsetProvider
from matrix.int.vector_store import VectorStore


__all__ = ["LLM", "Embedder", "Storage", "ToolsetProvider", "VectorStore"]
