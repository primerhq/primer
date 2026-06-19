"""Abstract base class for the document content store.

The content store holds document BODIES in the primary database
(sqlite/postgres), keyed by the stable internal document id, with a
UNIQUE(collection_id, path) constraint that makes it authoritative for
path<->id resolution and path uniqueness. It is a sibling of
``Storage`` (which holds JSONB entity metadata) and shares the same
backend connection/pool. The vector store is a separate, optional index.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class ContentRow(BaseModel):
    document_id: str
    collection_id: str
    path: str
    content: str


class ContentListEntry(BaseModel):
    document_id: str
    path: str
    size: int  # len(content) in characters; never loads the body


class DocumentContentStore(ABC):
    """Body store keyed by stable document id, addressed by (collection_id, path)."""

    @abstractmethod
    async def ensure_schema(self) -> None:
        """Create the content table + indexes if absent. Idempotent."""

    @abstractmethod
    async def get(self, document_id: str, *, conn: Any | None = None) -> str | None:
        """Return body by stable id, or None."""

    @abstractmethod
    async def get_by_path(
        self, collection_id: str, path: str, *, conn: Any | None = None
    ) -> ContentRow | None:
        """Resolve (collection_id, path) -> row (incl. document_id + body), or None."""

    @abstractmethod
    async def resolve_id(
        self, collection_id: str, path: str, *, conn: Any | None = None
    ) -> str | None:
        """Resolve (collection_id, path) -> document_id without loading the body."""

    @abstractmethod
    async def upsert(
        self,
        *,
        document_id: str,
        collection_id: str,
        path: str,
        content: str,
        conn: Any | None = None,
    ) -> None:
        """Insert or replace the body for document_id. Raises ConflictError if
        (collection_id, path) is already taken by a DIFFERENT document_id."""

    @abstractmethod
    async def delete(self, document_id: str, *, conn: Any | None = None) -> None:
        """Remove by stable id. No-op if absent."""

    @abstractmethod
    async def move(
        self, document_id: str, new_path: str, *, conn: Any | None = None
    ) -> None:
        """Change the path of an existing document. Raises ConflictError on collision,
        NotFoundError if the document_id has no content row."""

    @abstractmethod
    async def list(
        self, collection_id: str, *, prefix: str | None = None
    ) -> list[ContentListEntry]:
        """List entries under an optional path prefix. NEVER selects the body."""
