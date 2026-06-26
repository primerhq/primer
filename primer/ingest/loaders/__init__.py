"""Concrete :class:`DocumentLoader` implementations.

Subpackage for the shipped loaders. The default loader is
:class:`DoclingLoader`, which requires the optional ``docling`` extra and
is imported lazily via :pep:`562` ``__getattr__`` so importing this
subpackage never pulls the heavy ingestion / OCR stack.

    from primer.ingest.loaders import DoclingLoader
    # or, equivalently, the parent-package re-export:
    from primer.ingest import DoclingLoader
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.ingest.loaders.docling import DoclingLoader


__all__ = ["DoclingLoader"]


def __getattr__(name: str):
    if name == "DoclingLoader":
        try:
            from primer.ingest.loaders.docling import DoclingLoader
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise ModuleNotFoundError(
                "DoclingLoader requires the optional 'docling' extra. "
                "Install it with: pip install 'primer-ai[docling]' "
                "(or 'primer-ai[full]')."
            ) from exc
        return DoclingLoader
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
