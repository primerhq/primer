"""Push-based file-watch probe for host-side workspaces using ``watchfiles``.

Replaces ``HostStatProbe`` (polling via ``os.stat``) with an inotify-driven
probe for local (non-containerised) workspaces.

``watchfiles.awatch`` is already a transitive dependency of the project
(pulled in via watchfiles which is available in the environment).

Usage::

    probe = HostInotifyProbe(root="/home/user/workspace")
    async for change in probe.watch(["src/main.py", "data/"]):
        print(change.path, change.event_type)
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

import watchfiles

from primer.bus.ws_watch_probe import Change, WatchProbe

logger = logging.getLogger(__name__)

# Map watchfiles.Change enum values to string event types.
_WATCHFILES_EVENT_MAP: dict[watchfiles.Change, str] = {
    watchfiles.Change.added: "create",
    watchfiles.Change.modified: "modify",
    watchfiles.Change.deleted: "delete",
}


class HostInotifyProbe(WatchProbe):
    """Watch-probe for host-side workspaces using ``watchfiles.awatch``.

    Monitors the workspace root directory and yields workspace-relative
    :class:`~matrix.bus.ws_watch_probe.Change` events as inotify (or
    equivalent OS-level) notifications arrive.

    Only changes to paths listed in the ``paths`` argument passed to
    :meth:`watch` are yielded; changes to other paths inside the root are
    silently discarded.

    Parameters
    ----------
    root:
        Absolute path of the workspace root on the host filesystem.
    """

    def __init__(self, *, root: str) -> None:
        self._root = root.rstrip("/")

    def _abs(self, rel: str) -> str:
        """Return the absolute host path for a workspace-relative path."""
        if os.path.isabs(rel):
            return rel
        return f"{self._root}/{rel}"

    def _rel(self, abs_path: str) -> str:
        """Strip the workspace root prefix; return workspace-relative path."""
        prefix = self._root + "/"
        if abs_path.startswith(prefix):
            return abs_path[len(prefix):]
        return abs_path

    async def watch(self, paths: list[str]) -> AsyncIterator[Change]:  # type: ignore[override]
        """Yield :class:`Change` events for the given workspace-relative paths.

        Watches the workspace root via ``watchfiles.awatch``.  Only events
        whose absolute path matches one of the requested *paths* are yielded.

        The iterator terminates if the caller closes it or if
        ``watchfiles.awatch`` raises (e.g. the directory is deleted).
        """
        # Build a set of absolute target paths for fast membership testing.
        abs_targets: set[str] = {self._abs(p) for p in paths}

        try:
            async for batch in watchfiles.awatch(self._root):
                for raw_change, abs_path in batch:
                    if abs_path not in abs_targets:
                        continue
                    event_type = _WATCHFILES_EVENT_MAP.get(raw_change, "modify")
                    rel = self._rel(abs_path)
                    yield Change(path=rel, event_type=event_type)
        except Exception as exc:  # noqa: BLE001
            logger.warning("HostInotifyProbe: watchfiles.awatch raised: %s", exc)


__all__ = ["HostInotifyProbe"]
