"""Concrete :class:`DocumentLoader` implementations.

Subpackage for the shipped loaders. The default loader is
:class:`DoclingLoader`, which ships with the core install
(``docling`` is a core dependency, not an optional extra).

    from matrix.ingest.loaders import DoclingLoader
    # or, equivalently, the parent-package re-export:
    from matrix.ingest import DoclingLoader
"""

from matrix.ingest.loaders.docling import DoclingLoader


__all__ = ["DoclingLoader"]
