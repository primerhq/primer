"""End-to-end semantic-search orchestration.

Public surface:

* :class:`CollectionSearcher` -- ties together :class:`Embedder`,
  :class:`VectorStore`, and optionally :class:`CrossEncoder` to run
  the per-collection :attr:`Collection.search` config.
"""

from primer.search.searcher import CollectionSearcher


__all__ = ["CollectionSearcher"]
