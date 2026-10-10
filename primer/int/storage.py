"""Abstract base class for storage backends.

Sibling of :class:`primer.int.LLM`, :class:`primer.int.Embedder`, and
:class:`primer.int.ToolsetProvider`. Each :class:`Storage` instance is
bound to one model type (the type parameter ``ModelT`` must inherit from
:class:`primer.model.common.Identifiable`) and one backend (in-memory,
SQLite, Postgres, MongoDB, etc.). One backend instance, one model type:
applications that store multiple model kinds wire up one
:class:`Storage` per kind.

The interface exposes eight operations:

* :meth:`Storage.get` -- fetch by id, returns ``None`` if missing.
* :meth:`Storage.create` -- insert a new entity, raise
  :class:`primer.model.except_.ConflictError` on duplicate id.
* :meth:`Storage.update` -- replace an existing entity, raise
  :class:`primer.model.except_.NotFoundError` if missing.
* :meth:`Storage.update_unless` -- like ``update``, but atomically
  skipped if the row's CURRENT value of a given field already equals a
  forbidden value -- for callers that must not act on a stale snapshot.
* :meth:`Storage.patch_if` -- a field-scoped compare-and-set: merge a
  shallow ``patch`` and nested ``set_paths`` leaves iff every ``where``
  clause matches the row's CURRENT document, in one statement. For
  writers that own only a few fields and must not overwrite the rest.
* :meth:`Storage.delete` -- remove by id, raise
  :class:`primer.model.except_.NotFoundError` if missing.
* :meth:`Storage.list` -- paginated enumeration, optionally ordered.
* :meth:`Storage.find` -- paginated query with predicate filter,
  optionally ordered. ``predicate=None`` is equivalent to ``list``.

Pagination is bidirectional: callers supply either an
:class:`primer.model.storage.OffsetPage` or a
:class:`primer.model.storage.CursorPage` request and receive the
matching response shape. Backends MUST support both styles; backends
that don't natively offer offset (some KV stores) emulate by
materialising-and-slicing.

The predicate language is a binary expression tree -- see
:class:`primer.model.storage.Predicate`. Backends are free to optimise
common operator/operand combinations natively (e.g. compile the tree
to a SQL ``WHERE`` clause) but MUST always evaluate the same logical
semantics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, Generic, TypeVar

from primer.model.common import Identifiable
from primer.model.storage import (
    CursorPageResponse,
    OffsetPageResponse,
    OrderBy,
    PageRequest,
    Predicate,
)


ModelT = TypeVar("ModelT", bound=Identifiable)


class Storage(ABC, Generic[ModelT]):
    """Provider-agnostic CRUD + search interface for one model type.

    Subclasses bind to a backend and to a single ``ModelT``. Callers
    receive concrete instances (e.g. a ``Storage[Document]`` from a
    repository factory) and use them without knowing which backend is
    on the other side.
    """

    @abstractmethod
    async def get(self, id: str, *, conn: Any | None = None) -> ModelT | None:
        """Fetch the entity with the given id, or ``None`` if missing.

        Distinguishes "not found" from "lookup failed" by returning
        ``None`` for the former and raising for the latter (network /
        backend errors propagate).

        Parameters
        ----------
        conn
            When provided, read on that backend connection instead of
            acquiring one from the pool. Lets a caller read inside a
            transaction it already opened. Pool-less backends (SQLite,
            in-memory) ignore it.
        """

    @abstractmethod
    async def create(self, entity: ModelT, *, conn: Any | None = None) -> ModelT:
        """Insert a new entity.

        Returns the stored entity (which may differ from the input if
        the backend assigns auto-populated fields, e.g. timestamps).

        Parameters
        ----------
        conn
            When provided, write on that backend connection/transaction
            instead of acquiring one from the pool. Lets a caller commit
            the insert atomically with other work on the same
            transaction. Pool-less backends (SQLite, in-memory) ignore
            it.

        Raises
        ------
        primer.model.except_.ConflictError
            An entity with the same id already exists.
        """

    @abstractmethod
    async def update(self, entity: ModelT, *, conn: Any | None = None) -> ModelT:
        """Replace the entity matching ``entity.id`` with the given value.

        Returns the stored entity post-update.

        Parameters
        ----------
        conn
            When provided, write on that backend connection/transaction
            instead of acquiring one from the pool. Lets a caller commit
            the write atomically with other work on the same
            transaction. Pool-less backends (SQLite, in-memory) ignore
            it.

        Raises
        ------
        primer.model.except_.NotFoundError
            No entity with this id exists.
        """

    @abstractmethod
    async def update_unless(
        self, entity: ModelT, *, field: str, forbidden: Any,
        conn: Any | None = None,
    ) -> ModelT | None:
        """Like :meth:`update`, but atomically skipped if the ROW'S OWN
        current ``field`` value equals ``forbidden`` at write time.

        Closes a fetch-then-write race that a caller-side "is this row
        still eligible" check on its own snapshot cannot: the guard is
        evaluated by the backend against the CURRENT stored row in the
        same statement as the write, not against whatever the caller
        read earlier. Use this instead of ``get``/``find`` + ``update``
        whenever the write must not silently resurrect or corrupt a row
        that transitioned out of eligibility in the gap between the
        caller's read and this call.

        Parameters
        ----------
        field
            A top-level field name on ``ModelT`` (dotted paths are not
            supported - the common case is a flat status-like field).
        forbidden
            The value that, if currently stored in ``field``, rejects
            the write.
        conn
            See :meth:`update`.

        Returns
        -------
        ModelT | None
            The stored entity post-update, or ``None`` when the row's
            current ``field`` already equals ``forbidden`` (the write
            was skipped; the row is unchanged).

        Raises
        ------
        primer.model.except_.NotFoundError
            No entity with this id exists.
        """

    @abstractmethod
    async def patch_if(
        self,
        id: str,
        patch: Mapping[str, Any] | None = None,
        *,
        where: Mapping[str | tuple[str, ...], Sequence[Any]],
        set_paths: Mapping[tuple[str, ...], Any] | None = None,
        conn: Any | None = None,
    ) -> ModelT | None:
        """Write ONLY the named fields of one row, iff its CURRENT document matches ``where``.

        The write is ONE guarded statement, evaluated by the backend against the row's current
        version: there is no read-modify-write, so a concurrent writer's other fields (a cancel
        flag, a human reply accumulated into a nested map) are never overwritten, which a
        whole-document :meth:`update` from a snapshot cannot promise. When what the patch wrote is
        not the model's own canonical spelling (``"5"`` for an int, a ``+00:00`` timestamp), a
        second statement in the same transaction rewrites the touched top-level fields to the
        canonical dump, so the stored document always equals what a read re-dumps and a guard built
        from :func:`primer.storage.raw_generation` matches.

        Parameters
        ----------
        patch
            Top-level fields to set, merged shallowly into the stored document (a value replaces
            that field wholesale). JSON-ready values, as ``model_dump(mode="json")`` produces.
        where
            ``{field: [allowed values, ...]}``, ALL fields must match (AND), any listed value
            matches (OR). Values are compared as typed JSON scalars, not as text, so ``True`` does
            not match ``"true"``. ``None`` in the list matches an absent field or JSON null. Take the
            comparison value from :func:`primer.storage.raw_generation`, never from a re-formatted
            Python value, so "the row I read" is spelled exactly as the backend stores it. A guard
            that names the DEFAULT of a field that cannot hold null also matches a document lacking
            the key (the model reads it as the default); a nullable field is compared as stored.
            A key may also be a PATH to a nested leaf, a tuple held to the rules of a ``set_paths``
            path (``("parked_state", "resume_event_payloads", key): [None]``: that leaf is absent):
            it is read one object key at a time, so an absent or non-object parent makes the leaf
            absent and a path never indexes into an array, and the leaf is compared like a field. A
            one-element path is the field itself.
        set_paths
            ``{("parked_state", "resume_event_payloads", key): value}``: nested leaves set after the
            shallow patch. Every parent is ensured to be an object first, shallowest first (an
            absent, null or scalar parent is replaced by ``{}``), so sibling keys written by others
            survive. Paths are at most four deep; elements may not contain a quote, a backslash or a
            control character (rejected identically on every backend).
        conn
            See :meth:`update`.

        Returns
        -------
        ModelT | None
            The stored entity after the write; ``None`` when ``where`` did not match (the row is
            unchanged).

        Raises
        ------
        primer.model.except_.NotFoundError
            No entity with this id exists. A missing row is NOT reported as ``None``: "someone else
            won the race" and "the row is gone" need different handling.
        primer.storage.PatchSpecError
            The caller's spec is malformed (a ``ValueError`` subclass, so a handler for ``ValueError``
            still catches it). Among others: an empty ``patch``/``set_paths`` or an empty ``where``
            (or an empty allowed-value list in one); a patch key or path root that is ``id`` or, unless
            the model allows extras, not a field of the model; a ``where`` that names ``id``; a root
            named in both ``patch`` and ``set_paths``; a key that is empty or not a string, or a
            ``set_paths`` key that is not a tuple; a bad key or path (forbidden characters, too deep,
            a prefix of another); a ``where`` value that is not a list of JSON scalars (a bare string
            is rejected); a value that is not strict JSON (``NaN``, ``Infinity``) or holds a string
            that cannot be UTF-8 encoded (a surrogate code point); more than 32 patch keys, 16 leaves
            or 4 distinct parent objects. Rejected identically on every backend, before any SQL. Two
            more are raised only AFTER the guarded write, so only a caller whose guard matched sees
            them: a ``set_paths`` leaf the validated model does not carry (a typo under a typed sub-model),
            and a value the model turns into something strict JSON cannot hold (the string ``"nan"``
            written into a float field); that write is rolled back. The refusals about a supplied VALUE (not
            strict JSON, not encodable as UTF-8, in a value or a key, or such a canonical value) are the
            subclass ``primer.storage.PatchValueError``; the rest are about the spec's shape.
        pydantic.ValidationError
            A document fails model validation: the stored row is unreadable, or the document the
            write would produce no longer validates against the model. The write is rolled back (a
            savepoint inside a caller's transaction) and the row is unchanged. It is a ``ValueError``
            subclass but NOT a ``PatchSpecError``, and it is re-raised as itself, never wrapped, so a
            caller can tell the three apart: a malformed SPEC is a ``PatchSpecError`` that is not a
            ``PatchValueError``, a VALUE no backend can store is a ``PatchValueError``, and a value
            the model refuses is this ``ValidationError``.
        primer.model.except_.ProviderError
            Any OTHER failure under the write is a backend failure, wrapped like every other one and
            never reported as the caller's mistake: a ``ValueError`` that is neither a
            ``PatchSpecError`` nor a ``ValidationError`` (a stored document that cannot be read back, a
            driver quirk) is a ``ProviderError`` on both backends. A database error is a
            ``ProviderError`` from SQLite (a ``ServerError``, its subclass, for a
            ``sqlite3.OperationalError`` such as a locked database) and a ``ServerError`` from
            Postgres (any ``asyncpg.PostgresError``; a non-database failure is a ``ProviderError``).

        Comparison rules for ``where``: typed JSON scalars, numbers by value (``1`` equals ``1.0``),
        a number is never a string or a bool, ``None`` matches an absent field or JSON null.
        """

    @abstractmethod
    async def delete(self, id: str, *, conn: Any | None = None) -> None:
        """Remove the entity with the given id.

        Parameters
        ----------
        conn
            When provided, delete on that backend connection/transaction
            instead of acquiring one from the pool. Lets a caller commit
            the delete atomically with other work on the same
            transaction. Pool-less backends (SQLite, in-memory) ignore
            it.

        Raises
        ------
        primer.model.except_.NotFoundError
            No entity with this id exists. Callers that want
            idempotent semantics should suppress the exception.
        """

    @abstractmethod
    async def list(
        self,
        page: PageRequest,
        *,
        order_by: list[OrderBy] | None = None,
    ) -> OffsetPageResponse[ModelT] | CursorPageResponse[ModelT]:
        """Paginated enumeration of every entity in the store.

        Parameters
        ----------
        page
            Either an :class:`OffsetPage` or a :class:`CursorPage`. The
            response shape mirrors the request: offset request -> offset
            response; cursor request -> cursor response.
        order_by
            Sort keys applied left-to-right. ``None`` lets the backend
            choose a default order, but cursor pagination requires a
            stable total ordering -- backends MUST add an implicit
            secondary sort by ``id`` when the supplied ``order_by`` is
            non-unique. Rows whose sort key is NULL sort LAST on every
            backend, and keyset (cursor) pagination MUST page across the
            NULL boundary without dropping or duplicating rows.

        Returns
        -------
        OffsetPageResponse[ModelT] | CursorPageResponse[ModelT]
            Type matches the request's pagination kind.
        """

    @abstractmethod
    async def find(
        self,
        predicate: Predicate | None,
        page: PageRequest,
        *,
        order_by: list[OrderBy] | None = None,
    ) -> OffsetPageResponse[ModelT] | CursorPageResponse[ModelT]:
        """Paginated search filtered by a predicate.

        Parameters
        ----------
        predicate
            The filter to apply. ``None`` is equivalent to
            :meth:`list` -- accepted as a convenience so callers don't
            have to branch.
        page, order_by
            See :meth:`list`.

        Returns
        -------
        OffsetPageResponse[ModelT] | CursorPageResponse[ModelT]
            Type matches the request's pagination kind.

        Raises
        ------
        primer.model.except_.BadRequestError
            The predicate references a field the backend cannot
            translate, or uses an operand layout the backend does not
            support (e.g. column-vs-column comparison on a backend
            that requires literal-on-the-right).
        """
