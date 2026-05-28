"""Concrete :class:`DocumentSplitter` implementations.

Subpackage for the shipped splitters. The default splitter is
:class:`DoclingSplitter` (structure-aware, paired with
:class:`DoclingLoader`); :class:`RecursiveSplitter` is a pure-Python
fallback for callers that want no external parser involvement.

    from primer.ingest.splitters import DoclingSplitter, RecursiveSplitter
    # or, equivalently, the parent-package re-exports:
    from primer.ingest import DoclingSplitter, RecursiveSplitter
"""

from primer.ingest.splitters.docling import DoclingSplitter
from primer.ingest.splitters.recursive import RecursiveSplitter


__all__ = ["DoclingSplitter", "RecursiveSplitter"]
