"""Build the document/secret resolver callables for ``resolve_file_sources``.

The shared :func:`primer.workspace.files.resolve_file_sources` takes a
``document_resolver`` and a ``secret_resolver`` (each
``Callable[[FileMount], Awaitable[bytes]]``). These factories adapt the
``StorageProvider`` and ``SecretProvider`` to that contract. The
orchestration layer (``WorkspaceRegistry.materialise``) calls them once
per materialisation and passes the results down via ``FileResolvers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from collections.abc import Awaitable, Callable

from primer.model.collection import Collection, Document

if TYPE_CHECKING:
    from primer.int.secret_provider import SecretProvider
    from primer.access.person import Person
    from primer.int.storage_provider import StorageProvider
    from primer.model.workspace import FileMount

_Resolver = Callable[["FileMount"], Awaitable[bytes]]


async def _document_unreadable(storage_provider: "StorageProvider", person: "Person", doc: Document) -> bool:
    """Whether ``person`` holds no READ on the collection ``doc`` lives in (a document follows its collection; admins read everything)."""
    from primer.access.policy import level_of

    if person.is_admin:
        return False
    collection = await storage_provider.get_storage(Collection).get(doc.collection_id)
    return collection is None or level_of(person, collection.access) is None


async def refuse_unreadable_document_mounts(storage_provider: "StorageProvider", person: "Person", mounts: "list[FileMount]") -> None:
    """Raise ``NotFoundError`` for the first ``document`` file source ``person`` cannot read (access-control P3 SEC, finding 4).

    A template's author is the SAVER: a mount of a document they cannot read is refused when they save it, like a missing document, and never
    names the collection the document really lives in.
    """
    from primer.model.except_ import NotFoundError

    for fm in mounts:
        src = fm.source
        if getattr(src, "kind", None) != "document":
            continue
        doc = await storage_provider.get_storage(Document).get(src.document_id)
        if doc is None or await _document_unreadable(storage_provider, person, doc):
            raise NotFoundError(f"FileSource path={fm.path!r} kind=document: document {src.document_id!r} not found")


def make_document_resolver(storage_provider: "StorageProvider", person: "Person | None" = None) -> _Resolver:
    """Return a resolver that reads a Document's body from storage.

    With a ``person`` (the creator of the workspace, or the saver of the template) the document's collection must be readable by them, else the document
    answers as missing (access-control P3 SEC, finding 4); without one (the bootstrap seed, an internal caller) nothing is checked.

    Loads the Document by ``source.document_id``, verifies it belongs to
    ``source.collection_id``, then reads the body from the content store
    (keyed by the stable document id) and returns it UTF-8 encoded. The
    content store is the single body location (S2), so a document with no
    content row has no body. Raises ``RuntimeError`` (surfacing as a
    workspace-create failure) on missing document, collection mismatch, or
    empty body.
    """

    async def _resolve(fm: "FileMount") -> bytes:
        src = fm.source
        doc = await storage_provider.get_storage(Document).get(src.document_id)
        if doc is None or (person is not None and await _document_unreadable(storage_provider, person, doc)):
            raise RuntimeError(
                f"FileSource path={fm.path!r} kind=document: "
                f"document {src.document_id!r} not found"
            )
        if doc.collection_id != src.collection_id:
            # Does not name the collection the document really lives in (it may be one the creator cannot read).
            raise RuntimeError(
                f"FileSource path={fm.path!r} kind=document: document "
                f"{src.document_id!r} is not in collection {src.collection_id!r}"
            )
        body = await storage_provider.get_content_store().get(doc.id)
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
