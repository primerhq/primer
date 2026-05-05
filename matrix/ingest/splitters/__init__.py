"""Concrete :class:`DocumentSplitter` implementations.

Subpackage for the shipped splitters. The default splitter is
:class:`DoclingSplitter` (structure-aware, paired with
:class:`DoclingLoader`); :class:`RecursiveSplitter` is a pure-Python
fallback for callers that want no external parser involvement.

    from matrix.ingest.splitters import DoclingSplitter, RecursiveSplitter
    # or, equivalently, the parent-package re-exports:
    from matrix.ingest import DoclingSplitter, RecursiveSplitter
"""

from matrix.ingest.splitters.docling import DoclingSplitter
from matrix.ingest.splitters.recursive import RecursiveSplitter


__all__ = ["DoclingSplitter", "RecursiveSplitter"]
