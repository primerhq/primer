"""Provider-agnostic interface ABCs.

Three abstract base classes are exported:

* :class:`LLM` -- streaming chat interface (text/multimodal in,
  :class:`StreamEvent` out).
* :class:`Embedder` -- embedding interface (multimodal in,
  :class:`EmbedResponse` out).
* :class:`ToolsetProvider` -- toolset interface (list and invoke tools).

See ``research/abc_interface.md`` for the design rationale and
per-provider adapter mapping rules.
"""

from matrix.int.embedder import Embedder
from matrix.int.llm import LLM
from matrix.int.toolset import ToolsetProvider


__all__ = ["LLM", "Embedder", "ToolsetProvider"]
