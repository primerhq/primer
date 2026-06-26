"""Concrete :class:`DocumentSplitter` implementations.

Subpackage for the shipped splitters. The default splitter is
:class:`DoclingSplitter` (structure-aware, paired with
:class:`DoclingLoader`), which requires the optional ``docling`` extra
and is imported lazily via :pep:`562` ``__getattr__``;
:class:`RecursiveSplitter` is a pure-Python, zero-dependency fallback.

    from primer.ingest.splitters import DoclingSplitter, RecursiveSplitter
    # or, equivalently, the parent-package re-exports:
    from primer.ingest import DoclingSplitter, RecursiveSplitter
"""

from typing import TYPE_CHECKING

from primer.ingest.splitters.recursive import RecursiveSplitter

if TYPE_CHECKING:
    from primer.ingest.splitters.docling import DoclingSplitter


__all__ = ["DoclingSplitter", "RecursiveSplitter"]


def __getattr__(name: str):
    if name == "DoclingSplitter":
        try:
            from primer.ingest.splitters.docling import DoclingSplitter
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise ModuleNotFoundError(
                "DoclingSplitter requires the optional 'docling' extra. "
                "Install it with: pip install 'primer-ai[docling]' "
                "(or 'primer-ai[full]')."
            ) from exc
        return DoclingSplitter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
