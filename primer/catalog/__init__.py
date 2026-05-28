"""Internal semantic catalog over Describeable entities.

Public surface:

* :class:`SemanticCatalog` — orchestrates per-type vector indexing
  for :class:`Agent`, :class:`Tool`, :class:`Graph`, and
  :class:`Collection` entities.
* :class:`SemanticEntityType` — enum identifying the four
  catalog-indexable entity types.
* :class:`SemanticHit` — per-result return type from
  :meth:`SemanticCatalog.search`.

See ``docs/superpowers/specs/2026-05-08-semantic-catalog-design.md``
for the surrounding design.
"""

from primer.catalog.catalog import SemanticCatalog
from primer.catalog.types import SemanticEntityType, SemanticHit


__all__ = [
    "SemanticCatalog",
    "SemanticEntityType",
    "SemanticHit",
]
