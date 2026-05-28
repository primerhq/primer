"""Watch op handler for the workspace runtime.

Uses ``watchfiles`` (inotify-backed on Linux) to push file-change events to
subscribers.  ``aionotify`` was evaluated but not available in the dev
environment; ``watchfiles`` is a mature, well-maintained alternative.

Protocol
--------
Incoming::

    {"req_id": 9, "op": "watch_start",
     "args": {"paths": ["rel/path", "rel/glob/*"], "events": ["modify","delete"]}}

Outgoing (streaming)::

    {"req_id": 9, "event": "watch_open"}
    {"req_id": 9, "event": "change", "path": "...", "mtime": ..., "size": ...}
    ...
    {"req_id": 9, "event": "watch_closed"}   ← on cancel or WS close

Cancel::

    {"req_id": 10, "op": "watch_cancel", "args": {"target_req_id": 9}}
    ← {"req_id": 9, "event": "watch_closed"}

Each subscription runs in its own asyncio Task.  ``watch_cancel`` looks up
the subscription by ``target_req_id`` and cancels its task.
"""

from __future__ import annotations

import asyncio
import fnmatch
import glob
import logging
import os
import pathlib
from collections.abc import Callable, Coroutine
from typing import Any

from watchfiles import Change, awatch

from primer_runtime.ops import OpError, _resolve_safe
from primer_runtime.protocol import ErrorCode, Event, serialize

log = logging.getLogger(__name__)

# Lower debounce so events arrive quickly (default is 1600 ms which is too
# slow for the <200 ms test budget).
_DEBOUNCE_MS = 50

# Map protocol event-name strings to watchfiles Change enum members.
_CHANGE_MAP: dict[str, Change] = {
    "create": Change.added,
    "modify": Change.modified,
    "delete": Change.deleted,
    "move": Change.added,  # watchfiles treats renames as added + deleted
}

# All watchfiles change types.
_ALL_CHANGES: frozenset[Change] = frozenset(Change)


# ---------------------------------------------------------------------------
# Subscription registry
# ---------------------------------------------------------------------------


class WatchRegistry:
    """Per-connection registry that maps req_id → running subscription task."""

    def __init__(self) -> None:
        self._subs: dict[int, asyncio.Task[None]] = {}

    def add(self, req_id: int, task: asyncio.Task[None]) -> None:
        self._subs[req_id] = task

    def cancel(self, req_id: int) -> bool:
        """Cancel and remove the subscription for *req_id*.

        Returns ``True`` if the subscription existed, ``False`` otherwise.
        """
        task = self._subs.pop(req_id, None)
        if task is None:
            return False
        task.cancel()
        return True

    def cancel_all(self) -> None:
        """Cancel all active subscriptions (called on WS close)."""
        for task in list(self._subs.values()):
            task.cancel()
        self._subs.clear()


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------


def _resolve_watch_paths(
    raw_patterns: list[str],
    workspace_root: str,
) -> tuple[list[str], list[str]]:
    """Expand glob patterns (relative to *workspace_root*) to real paths.

    Returns a pair ``(watch_dirs, raw_patterns_resolved)`` where:
    - ``watch_dirs``  — unique parent dirs to pass to ``awatch``
    - ``raw_patterns_resolved``  — the patterns with workspace_root prepended
      (used for post-filtering events by path)

    Raises :class:`~ops.OpError` with ``EACCES`` if any pattern escapes the
    workspace root.
    """
    root = pathlib.Path(workspace_root).resolve()
    watch_dirs: set[str] = set()
    resolved_patterns: list[str] = []

    for pattern in raw_patterns:
        # Safety: resolve the non-glob prefix to check it stays inside root.
        # For a glob like "foo/*/bar.txt" we check the anchor "foo/".
        anchor = _glob_anchor(pattern)
        _resolve_safe(anchor or ".", workspace_root)  # raises EACCES if unsafe

        # Build full pattern rooted at workspace_root
        if pathlib.PurePosixPath(pattern).is_absolute():
            full_pattern = pattern
        else:
            full_pattern = str(root / pattern)
        resolved_patterns.append(full_pattern)

        # Collect parent dirs (resolve any existing dirs that match so far)
        expanded = glob.glob(full_pattern, recursive=True)
        if expanded:
            for p in expanded:
                watch_dirs.add(str(pathlib.Path(p).parent))
        else:
            # Pattern points to something that doesn't exist yet; watch the
            # deepest existing ancestor.
            watch_dirs.add(str(_deepest_existing(pathlib.Path(full_pattern).parent, root)))

    return list(watch_dirs), resolved_patterns


def _glob_anchor(pattern: str) -> str:
    """Return the leading non-glob portion of *pattern* (without trailing /)."""
    parts = pathlib.PurePosixPath(pattern).parts
    anchor_parts = []
    for part in parts:
        if any(c in part for c in ("*", "?", "[")):
            break
        anchor_parts.append(part)
    return str(pathlib.PurePosixPath(*anchor_parts)) if anchor_parts else ""


def _deepest_existing(path: pathlib.Path, root: pathlib.Path) -> pathlib.Path:
    """Walk up from *path* until we find an existing directory, but stop at *root*."""
    current = path
    while current != root.parent:
        if current.exists():
            return current
        current = current.parent
        if current == root.parent:
            return root
    return root


# ---------------------------------------------------------------------------
# Event-mask helpers
# ---------------------------------------------------------------------------


def _build_change_mask(event_names: list[str] | None) -> frozenset[Change]:
    """Convert a list of protocol event names to a ``frozenset[Change]``."""
    if not event_names:
        return _ALL_CHANGES
    mask: set[Change] = set()
    for name in event_names:
        change = _CHANGE_MAP.get(name)
        if change is not None:
            mask.add(change)
    return frozenset(mask) if mask else _ALL_CHANGES


# ---------------------------------------------------------------------------
# Watch subscription coroutine
# ---------------------------------------------------------------------------


async def _run_subscription(
    req_id: int,
    raw_patterns: list[str],
    change_mask: frozenset[Change],
    workspace_root: str,
    send: Callable[[str], Coroutine[Any, Any, None]],
) -> None:
    """Async task body: watch paths and push change events until cancelled.

    ``send`` is an async callable (typically ``ws.send_str``) that accepts a
    serialised JSON frame.
    """
    root = pathlib.Path(workspace_root).resolve()

    try:
        watch_dirs, resolved_patterns = _resolve_watch_paths(raw_patterns, workspace_root)
    except OpError as exc:
        err_event = Event(
            req_id=req_id,
            event="watch_error",
            data={"code": exc.code, "message": exc.message},
        )
        await send(serialize(err_event))
        return

    if not watch_dirs:
        watch_dirs = [str(root)]

    # Announce watch_open immediately
    await send(serialize(Event(req_id=req_id, event="watch_open")))

    # Use a custom filter that lets ALL file types through (watchfiles
    # DefaultFilter silently drops .py files and others, which would break tests).
    def _no_filter(change: Change, path: str) -> bool:  # noqa: ARG001
        return True

    try:
        async for changes in awatch(
            *watch_dirs,
            watch_filter=_no_filter,
            debounce=_DEBOUNCE_MS,
            stop_event=None,
        ):
            for change_type, changed_path in changes:
                # Apply event-mask filter
                if change_type not in change_mask:
                    continue

                # Apply pattern filter — at least one pattern must match
                if not _matches_any(changed_path, resolved_patterns):
                    continue

                # Build the change event
                try:
                    st = os.stat(changed_path)
                    mtime: float | None = st.st_mtime
                    size: int | None = st.st_size
                except OSError:
                    mtime = None
                    size = None

                change_event = Event(
                    req_id=req_id,
                    event="change",
                    data={
                        "path": changed_path,
                        "mtime": mtime,
                        "size": size,
                    },
                )
                await send(serialize(change_event))

    except asyncio.CancelledError:
        # Normal cancellation path — fall through to watch_closed
        pass
    except Exception:
        log.exception("Unexpected error in watch subscription req_id=%d", req_id)

    # Always emit watch_closed when the task ends
    await send(serialize(Event(req_id=req_id, event="watch_closed")))


def _matches_any(path: str, patterns: list[str]) -> bool:
    """Return True if *path* matches at least one glob *pattern*."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        # Also match if path is directly inside a watched dir (pattern ends with /*)
        # or if the pattern has no glob chars (exact watch on a specific file or dir)
        if not any(c in pattern for c in ("*", "?", "[")):
            # Exact path or prefix match: accept if path == pattern
            # or path is inside the pattern as a dir
            if path == pattern or path.startswith(pattern.rstrip("/") + "/"):
                return True
    return False


# ---------------------------------------------------------------------------
# Public helpers called from server.py
# ---------------------------------------------------------------------------


def start_watch(
    req_id: int,
    args: dict[str, Any],
    workspace_root: str,
    send: Callable[[str], Coroutine[Any, Any, None]],
    registry: WatchRegistry,
) -> None:
    """Spawn a subscription task for a ``watch_start`` op.

    The task is registered in *registry* under *req_id*.  Call
    :func:`cancel_watch` (or ``registry.cancel``) to stop it.
    """
    raw_patterns: list[str] = args.get("paths") or ["."]
    event_names: list[str] | None = args.get("events")
    change_mask = _build_change_mask(event_names)

    task = asyncio.create_task(
        _run_subscription(req_id, raw_patterns, change_mask, workspace_root, send),
        name=f"watch:{req_id}",
    )
    registry.add(req_id, task)


def cancel_watch(
    target_req_id: int,
    registry: WatchRegistry,
) -> bool:
    """Cancel the subscription identified by *target_req_id*.

    Returns ``True`` if a subscription was found and cancelled.  The
    subscription task will emit a ``watch_closed`` event before it exits.
    """
    return registry.cancel(target_req_id)
