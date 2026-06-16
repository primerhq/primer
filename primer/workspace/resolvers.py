"""Build the document/secret resolver callables for ``resolve_file_sources``.

The shared :func:`primer.workspace.files.resolve_file_sources` takes a
``document_resolver`` and a ``secret_resolver`` (each
``Callable[[FileMount], Awaitable[bytes]]``). These factories adapt the
``StorageProvider`` and ``SecretProvider`` to that contract. The
orchestration layer (``WorkspaceRegistry.materialise``) calls them once
per materialisation and passes the results down via ``FileResolvers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

from primer.knowledge.indexing import document_body_text
from primer.model.collection import Document

if TYPE_CHECKING:
    from primer.int.secret_provider import SecretProvider
    from primer.int.storage_provider import StorageProvider
    from primer.model.workspace import FileMount

_Resolver = Callable[["FileMount"], Awaitable[bytes]]


def make_document_resolver(storage_provider: "StorageProvider") -> _Resolver:
    """Return a resolver that reads a Document's body from storage.

    Loads the Document by ``source.document_id``, verifies it belongs to
    ``source.collection_id``, extracts its indexable body text, and
    returns it UTF-8 encoded. Raises ``RuntimeError`` (surfacing as a
    workspace-create failure) on missing document, collection mismatch,
    or empty body.
    """

    async def _resolve(fm: "FileMount") -> bytes:
        src = fm.source
        doc = await storage_provider.get_storage(Document).get(src.document_id)
        if doc is None:
            raise RuntimeError(
                f"FileSource path={fm.path!r} kind=document: "
                f"document {src.document_id!r} not found"
            )
        if doc.collection_id != src.collection_id:
            raise RuntimeError(
                f"FileSource path={fm.path!r} kind=document: document "
                f"{src.document_id!r} belongs to collection "
                f"{doc.collection_id!r}, not {src.collection_id!r}"
            )
        body = document_body_text(doc)
        if not body:
            raise RuntimeError(
                f"FileSource path={fm.path!r} kind=document: document "
                f"{src.document_id!r} has an empty body"
            )
        return body.encode("utf-8")

    return _resolve


def make_secret_resolver(secret_provider: "SecretProvider") -> _Resolver:
    """Return a resolver that reads a named secret from the provider.

    Raises ``RuntimeError`` naming the secret (never the value) when the
    provider returns ``None``.
    """

    async def _resolve(fm: "FileMount") -> bytes:
        src = fm.source
        value = await secret_provider.get_secret(src.name)
        if value is None:
            raise RuntimeError(
                f"FileSource path={fm.path!r} kind=secret: secret "
                f"{src.name!r} not found"
            )
        return value.get_secret_value().encode("utf-8")

    return _resolve
