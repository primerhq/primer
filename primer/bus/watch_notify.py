"""Deterministic wake-on-write for ``watch_files`` parks.

When a workspace file is mutated through the REST API (rather than by an
agent process inside the workspace), there is no guarantee an inotify
side-effect will fire promptly — the API may be writing to a backend whose
filesystem events the host watcher cannot observe, or the write may race
the watcher's scan window. This module closes that gap: it lets the write
endpoint *explicitly* wake any ``watch_files``-parked session in the same
workspace whose watched paths match the written path.

It reuses the existing resume path end-to-end:

* the same parked-session query the :class:`~primer.bus.watcher.WatcherManager`
  uses (:func:`~primer.bus.watcher._find_active_watch_parks`), and
* the same change-payload shape the inotify watcher publishes
  (``{"changes": [{"path", "event_type", "mtime_after"}]}``) to the park's
  ``parked_event_key``,

so the published event flows through the unchanged
:class:`~primer.bus.listener.YieldEventListener`, flipping the park
``parked -> resumable`` exactly as an inotify-driven change would. inotify
stays as the backstop; this just makes a Studio edit deterministic.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import PurePosixPath

from primer.bus.watcher import _find_active_watch_parks
from primer.int.event_bus import EventBus


logger = logging.getLogger(__name__)


def _normalise(path: str) -> str:
    """Normalise a workspace-relative path to forward slashes, no leading
    ``./`` and no surrounding slashes — the form the watched globs use."""
    p = path.replace("\\", "/").strip("/")
    if p.startswith("./"):
        p = p[2:]
    return p


def path_matches_watch(path: str, watched: str) -> bool:
    """Does ``path`` (workspace-relative) fall under the watched spec?

    ``watched`` may be:

    * an exact file (``src/app.py``),
    * a glob (``src/*.py``, ``**/*.ts``), or
    * a directory (``src``) — which matches anything beneath it, mirroring
      how the inotify probe watches a directory recursively.

    Matching is workspace-relative and POSIX-flavoured.
    """
    p = _normalise(path)
    w = _normalise(watched)
    if not w:
        # An empty / "." watch covers the whole workspace.
        return True
    if p == w:
        return True
    # Recursive glob (``**``) crosses path separators — fnmatch's ``*``
    # already spans ``/``, so it is the right tool for these patterns.
    if "**" in w:
        if fnmatch(p, w):
            return True
    else:
        # Segment-aware glob: ``src/*.py`` matches ``src/app.py`` but NOT
        # ``src/sub/app.py``. PurePosixPath.match keeps ``*`` inside a
        # single path segment (unlike fnmatch).
        try:
            if PurePosixPath(p).match(w):
                return True
        except ValueError:
            pass
    # Directory watch: ``src`` matches ``src/anything`` (any depth). Only
    # applies when the watched spec carries no glob metacharacters — a
    # glob like ``src/*`` is handled above and must NOT also match nested
    # files it doesn't literally cover.
    if not any(ch in w for ch in "*?["):
        if p.startswith(w + "/"):
            return True
    return False


def _change_payload(path: str) -> dict:
    """Build the same change-batch payload shape the inotify watcher
    publishes for a single modified file."""
    return {
        "changes": [
            {
                "path": _normalise(path),
                "event_type": "modified",
                "mtime_after": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }


async def wake_watch_files_on_write(
    *,
    workspace_id: str,
    path: str,
    scheduler,
    event_bus: EventBus,
) -> int:
    """Wake every ``watch_files``-parked session in ``workspace_id`` whose
    watched paths match the just-written ``path``.

    Queries the scheduler for active ``watch:*`` parks (the same query the
    :class:`~primer.bus.watcher.WatcherManager` uses), filters to this
    workspace, glob-matches ``path`` against each park's watched ``paths``,
    and on a match publishes the inotify-shaped change payload to the
    park's ``parked_event_key``. The unchanged
    :class:`~primer.bus.listener.YieldEventListener` does the
    ``parked -> resumable`` flip.

    Returns the number of parks woken (for tests / logging). Never raises
    on a per-park publish failure beyond what the bus itself surfaces; the
    caller is expected to treat the whole call as best-effort.
    """
    parks = await _find_active_watch_parks(scheduler)
    woken = 0
    for park in parks:
        if park.get("workspace_id") != workspace_id:
            continue
        event_key = park.get("event_key")
        if not event_key or not event_key.startswith("watch:"):
            continue
        watched_paths = park.get("paths") or []
        if not any(path_matches_watch(path, w) for w in watched_paths):
            continue
        await event_bus.publish(event_key, _change_payload(path))
        woken += 1
    return woken


__all__ = [
    "path_matches_watch",
    "wake_watch_files_on_write",
]
