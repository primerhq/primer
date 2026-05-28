"""Push-based file-watch probe backed by the workspace runtime WebSocket.

Replaces ``SandboxStatProbe`` (polling via ``docker exec``) with a
subscription-based probe that forwards inotify events pushed by the
in-container runtime server.

Usage::

    probe = WSWatchProbe(
        runtime_client=client,
        workspace_root="/workspace",
    )
    async for change in probe.watch(["src/main.py", "data/"]):
        print(change.path, change.event_type)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matrix.workspace.runtime.runtime_client import RuntimeClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared Change dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """A single filesystem change event yielded by a push-based watch probe.

    ``path`` is *workspace-relative* (i.e. without the workspace root prefix).
    ``event_type`` is one of ``"modify"``, ``"create"``, ``"delete"``.
    ``mtime`` and ``size`` are optional runtime-provided metadata.
    """

    path: str
    event_type: str  # "modify" | "create" | "delete"
    mtime: float | None = None
    size: int | None = None


# ---------------------------------------------------------------------------
# WatchProbe protocol
# ---------------------------------------------------------------------------


class WatchProbe:
    """Protocol / base class for push-based watch probes.

    Concrete implementations override :meth:`watch` to yield
    :class:`Change` events as they arrive from the underlying backend
    (inotify, WebSocket runtime events, …).
    """

    async def watch(self, paths: list[str]) -> AsyncIterator[Change]:  # pragma: no cover
        """Yield :class:`Change` events for the given workspace-relative paths.

        The iterator runs indefinitely until the caller closes it (e.g. via
        ``break`` or ``aclose()``) or an unrecoverable backend error occurs.
        """
        raise NotImplementedError
        # satisfy type-checker — unreachable
        yield Change(path="", event_type="modify")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# WSWatchProbe
# ---------------------------------------------------------------------------


class WSWatchProbe(WatchProbe):
    """Watch-probe that subscribes to the in-container runtime via WebSocket.

    Translates :class:`~matrix.workspace.runtime.runtime_client.ChangeEvent`
    objects (which carry absolute paths) into workspace-relative
    :class:`Change` objects by stripping the ``workspace_root`` prefix.

    Parameters
    ----------
    runtime_client:
        A connected :class:`~matrix.workspace.runtime.runtime_client.RuntimeClient`.
    workspace_root:
        Absolute path of the workspace inside the container
        (e.g. ``"/workspace"``).  Used to build absolute paths for the
        ``watch_start`` subscription and to strip the prefix from incoming
        events.
    """

    def __init__(
        self,
        *,
        runtime_client: "RuntimeClient",
        workspace_root: str,
    ) -> None:
        self._runtime_client = runtime_client
        self._workspace_root = workspace_root.rstrip("/")

    def _abs(self, rel: str) -> str:
        """Return the absolute container path for a workspace-relative path."""
        if rel.startswith("/"):
            return rel
        return f"{self._workspace_root}/{rel}"

    def _rel(self, abs_path: str) -> str:
        """Strip the workspace root prefix; return workspace-relative path."""
        prefix = self._workspace_root + "/"
        if abs_path.startswith(prefix):
            return abs_path[len(prefix):]
        # Already relative or outside root — return as-is.
        return abs_path

    async def watch(self, paths: list[str]) -> AsyncIterator[Change]:  # type: ignore[override]
        """Yield :class:`Change` events for the given workspace-relative paths.

        Internally calls ``runtime_client.watch(abs_paths, events)`` and
        translates each incoming :class:`ChangeEvent` into a :class:`Change`.
        The iterator terminates when the underlying stream closes (e.g. on
        WS disconnect).
        """
        abs_paths = [self._abs(p) for p in paths]
        async for ce in self._runtime_client.watch(abs_paths, ["modify", "create", "delete"]):
            rel = self._rel(ce.path)
            yield Change(
                path=rel,
                event_type=ce.event,
                mtime=ce.mtime,
                size=ce.size,
            )


__all__ = ["Change", "WatchProbe", "WSWatchProbe"]
