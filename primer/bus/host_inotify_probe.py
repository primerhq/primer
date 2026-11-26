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

import watchfiles

from primer.bus.watch_notify import path_matches_watch
from primer.bus.ws_watch_probe import Change, WatchProbe

logger = logging.getLogger(__name__)

# Map watchfiles.Change enum values to string event types.
_WATCHFILES_EVENT_MAP: dict[watchfiles.Change, str] = {
    watchfiles.Change.added: "create",
    watchfiles.Change.modified: "modify",
    watchfiles.Change.deleted: "delete",
}

# The runtime's own default state tree (the same value
# primer/workspace/reserved.py refuses user writes to): its churn
# (every session log append) is the runtime's own traffic, not a user
# change. A custom ``state_path`` is not suppressed: the probe only
# sees the root, not the workspace object (review 2026-11-23, I2, M-5).
_STATE_PATH = ".state"


class HostInotifyProbe(WatchProbe):
    """Watch-probe for host-side workspaces using ``watchfiles.awatch``.

    Monitors the workspace root directory and yields workspace-relative
    :class:`~primer.bus.ws_watch_probe.Change` events as inotify (or
    equivalent OS-level) notifications arrive.

    Only changes that match one of the ``paths`` passed to :meth:`watch` are
    yielded, with the matcher ``watch_files`` shares with REST-write wakes
    (:func:`~primer.bus.watch_notify.path_matches_watch`): an exact file, a
    glob (``src/*.py``), a directory (anything beneath it) or ``"."`` (the
    whole workspace). Changes to other paths inside the root are silently
    discarded.

    Ceiling: the recursive root watch costs one inotify watch per directory
    of the workspace tree for as long as a stream or park is open. Narrowing
    it to the watched directories would miss a directory that is deleted and
    recreated. The watch still SEES the runtime's own state tree
    (``.state/``), but a whole-workspace (``.``) watch does not deliver its
    churn: a spec that names ``.state`` (or a path under it) opts in
    (review 2026-11-23, I2, M-5).

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

    def _spec(self, path: str) -> str:
        """A watch spec as workspace-relative text: absolute paths under the root made relative, ``.`` as the empty (whole-workspace) spec."""
        rel = self._rel(path) if os.path.isabs(path) else path
        rel = rel.replace("\\", "/").strip("/")
        if rel.startswith("./"):
            rel = rel[2:]
        return "" if rel == "." else rel

    def _state_churn(self, rel: str, specs: list[str]) -> bool:
        """Whether ``rel`` is the runtime's own state tree and the watch only asked for the whole workspace.

        A ``.`` (whole-workspace) watch suppresses ``.state/`` churn
        (review 2026-11-23, I2, M-5): the drain that consumes it
        (``read_batch``) already surfaces the session logs it backs, and
        REST-write wakes never fire there (writes into the tree are
        refused). A spec that names ``.state`` or a path under it opts
        in and is never suppressed.
        """
        if rel != _STATE_PATH and not rel.startswith(_STATE_PATH + "/"):
            return False
        if not any(spec == "" for spec in specs):
            return False
        return all(
            spec != _STATE_PATH and not spec.startswith(_STATE_PATH + "/")
            for spec in specs
        )

    async def watch(self, paths: list[str]) -> AsyncIterator[Change]:  # type: ignore[override]
        """Yield :class:`Change` events for the given workspace-relative paths.

        Watches the workspace root (recursively) via ``watchfiles.awatch``. A
        watch on a subdirectory would be tied to that directory's inode and
        go silent once it is deleted and recreated (``rm -rf out && mkdir
        out``), so the root is the one watch that survives a clean build.
        Only events whose workspace-relative path matches one of the requested
        *paths* are yielded. A ``.`` (whole-workspace) watch additionally
        suppresses the runtime's own ``.state/`` churn; a spec that names
        ``.state`` or a path under it opts back in (review 2026-11-23, I2,
        M-5).

        The iterator terminates if the caller closes it or if
        ``watchfiles.awatch`` raises (e.g. the directory is deleted).
        """
        specs = [self._spec(p) for p in paths]

        try:
            async for batch in watchfiles.awatch(self._root):
                for raw_change, abs_path in batch:
                    rel = self._rel(abs_path)
                    if rel == abs_path:
                        continue  # the root itself (or something outside it): not a workspace path
                    if not any(path_matches_watch(rel, spec) for spec in specs):
                        continue
                    if self._state_churn(rel, specs):
                        continue
                    event_type = _WATCHFILES_EVENT_MAP.get(raw_change, "modify")
                    yield Change(path=rel, event_type=event_type)
        except Exception as exc:  # noqa: BLE001
            logger.warning("HostInotifyProbe: watchfiles.awatch raised: %s", exc)


__all__ = ["HostInotifyProbe"]
