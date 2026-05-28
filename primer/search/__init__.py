"""End-to-end semantic-search orchestration.

Public surface:

* :class:`CollectionSearcher` -- ties together :class:`Embedder`,
  :class:`VectorStore`, and optionally :class:`CrossEncoder` to run
  the per-collection :attr:`Collection.search` config (MMR + CER).

See ``docs/superpowers/specs/2026-05-05-mmr-cross-encoder-reranking-design.md``
for the surrounding design.
"""

from primer.search.searcher import CollectionSearcher


__all__ = ["CollectionSearcher"]
