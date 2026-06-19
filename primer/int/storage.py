"""Abstract base class for storage backends.

Sibling of :class:`primer.int.LLM`, :class:`primer.int.Embedder`, and
:class:`primer.int.ToolsetProvider`. Each :class:`Storage` instance is
bound to one model type (the type parameter ``ModelT`` must inherit from
:class:`primer.model.common.Identifiable`) and one backend (in-memory,
SQLite, Postgres, MongoDB, etc.). One backend instance, one model type:
applications that store multiple model kinds wire up one
:class:`Storage` per kind.

The interface exposes six operations:

* :meth:`Storage.get` -- fetch by id, returns ``None`` if missing.
* :meth:`Storage.create` -- insert a new entity, raise
  :class:`primer.model.except_.ConflictError` on duplicate id.
* :meth:`Storage.update` -- replace an existing entity, raise
  :class:`primer.model.except_.NotFoundError` if missing.
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
