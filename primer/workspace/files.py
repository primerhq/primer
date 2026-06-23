"""Resolve every ``FileSource`` variant into uniform ``ResolvedFile`` entries.

Backends (container, k8s) historically silently ``log.warning``-and-skipped
every non-inline ``FileSource`` variant (``url``, ``document``, ``secret``).
This helper centralises resolution in the platform process so each backend
just pushes the resulting bytes into its workspace (``RuntimeClient.write_file``
or equivalent).

Sources:

* ``inline``   -- content used as-is (UTF-8 encoded).
* ``url``      -- ``aiohttp`` GET; non-2xx raises.
* ``document`` -- delegated to ``document_resolver`` (storage-backed).
* ``secret``   -- delegated to ``secret_resolver``.

The resolvers receive the entire :class:`FileMount` so they can read both the
discriminated source fields and the destination ``path`` / ``mode``.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from collections.abc import Awaitable, Callable

import aiohttp

if TYPE_CHECKING:
    from primer.model.workspace import FileMount

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedFile:
    """A fully-resolved file ready to push into a workspace."""

    path: str
    """Workspace-relative destination path (verbatim from ``FileMount.path``)."""

    content: bytes
    """File body."""

    mode: str | None
    """Octal mode string (e.g. ``"0755"``) or ``None`` for the backend default."""


def _http_session() -> aiohttp.ClientSession:
    """Hook for tests to patch out the aiohttp session."""
    return aiohttp.ClientSession()


_DocumentResolver = Callable[["FileMount"], Awaitable[bytes]]
_SecretResolver = Callable[["FileMount"], Awaitable[bytes]]


@dataclass(frozen=True)
class FileResolvers:
    """Bundle of the optional resolvers passed into a backend's ``create``.

    Built by the orchestration layer (``WorkspaceRegistry.materialise``)
    and forwarded to :func:`resolve_file_sources`. ``None`` for either
    field means that source kind is unsupported in this call and will
    raise inside :func:`resolve_file_sources`.
    """

    document_resolver: _DocumentResolver | None = None
    secret_resolver: _SecretResolver | None = None


async def resolve_file_sources(
    mounts: list["FileMount"],
    *,
    document_resolver: _DocumentResolver | None = None,
    secret_resolver: _SecretResolver | None = None,
) -> list[ResolvedFile]:
    """Resolve every variant on the given mounts into ``ResolvedFile`` entries.

    Order is preserved; one input mount produces one output entry. An unknown
    ``kind`` is logged and dropped (defensive against future enum drift).
    """
    out: list[ResolvedFile] = []
    for fm in mounts:
        src = fm.source
        kind = getattr(src, "kind", None)

        if kind == "inline":
            content = src.content.encode("utf-8")
        elif kind == "url":
            url = str(src.url)
            async with _http_session() as session:
                async with session.get(url) as resp:
                    if resp.status >= 300:
                        raise RuntimeError(
                            f"FileSource url={url!r} returned {resp.status}"
                        )
                    content = await resp.read()
            if src.sha256 is not None:
                expected = src.sha256.lower()
                actual = hashlib.sha256(content).hexdigest()
                if actual != expected:
                    raise RuntimeError(
                        f"FileSource url={url!r} sha256 mismatch: "
                        f"expected {expected}, got {actual}"
                    )
        elif kind == "document":
            if document_resolver is None:
                raise RuntimeError(
                    f"FileSource path={fm.path!r} kind=document requires document_resolver"
                )
            content = await document_resolver(fm)
        elif kind == "secret":
            if secret_resolver is None:
                raise RuntimeError(
                    f"FileSource path={fm.path!r} kind=secret requires secret_resolver"
                )
            content = await secret_resolver(fm)
        else:
            logger.warning(
                "Unknown FileSource kind %r for path %s; skipping",
                kind,
                fm.path,
            )
            continue

        out.append(ResolvedFile(path=fm.path, content=content, mode=fm.mode))
    return out
