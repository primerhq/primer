"""Provider-agnostic interface ABCs.

Two abstract base classes are exported:

* :class:`LLM` — streaming chat interface (text/multimodal in,
  :class:`StreamEvent` out).
* :class:`Embedder` — embedding interface (multimodal in,
  :class:`EmbedResponse` out).

See ``research/abc_interface.md`` for the design rationale and
per-provider adapter mapping rules.
"""

from matrix.int.embedder import Embedder
from matrix.int.llm import LLM


__all__ = ["LLM", "Embedder"]
