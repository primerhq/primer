"""File-watcher backend for the ``watch_files`` yielding tool.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md`` §8.3.

Two classes:

* :class:`LocalWorkspaceWatcher` — the unit. Polls mtimes for a list
  of workspace-relative paths, fires an async ``on_change`` callback
  with a coalesced batch of change events whenever something shifts.
  No bus, no scheduler — pure file-system observation. Trivially
  unit-testable.
* :class:`WatcherManager` — the lifecycle owner. Periodically scans
  the scheduler for sessions parked on ``watch:*`` keys, starts a
  watcher per park, and stops watchers when the park flips to
  ``resumable`` or the deadline passes. Publishes change bursts on
  the event bus on behalf of each watcher.

Why a polling watcher instead of ``watchdog`` / inotify in v1?

* No new C-extension dependency.
* Predictable cross-platform behaviour (Windows handles inotify-
  equivalent APIs differently).
* The agent-driven workloads are typically watching a handful of
  paths for low-frequency changes — polling at 500ms is fine.

A ``watchdog`` migration is straightforward later: the
:class:`LocalWorkspaceWatcher` surface (constructor + start/stop +
on_change callback) doesn't change, only its insides.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from matrix.int.event_bus import EventBus

if TYPE_CHECKING:
    from matrix.scheduler.in_memory import InMemoryScheduler
    from matrix.scheduler.postgres import PostgresScheduler


logger = logging.getLogger(__name__)


DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_SCAN_INTERVAL_SECONDS = 2.0
DEFAULT_BATCH_WINDOW_MS = 250


# ===========================================================================
# LocalWorkspaceWatcher
# ===========================================================================


class LocalWorkspaceWatcher:
    """Polling watcher for a fixed list of workspace-relative paths.

    Each path is stat()ed every ``poll_interval_seconds``. When a
    change is detected (mtime delta, file appeared, or file
    disappeared), the watcher waits ``batch_window_ms`` for more
    changes, collects them, and fires ``on_change`` once with the
    full batch.

    A burst that crosses the batch window boundary is split — the
    watcher emits whatever it had at window-close, then re-arms the
    detection state and waits for the next change.

    Directories are stat()ed too: a directory's mtime changes when
    children are added / removed. For finer per-file events inside a
    directory the caller should list those children explicitly.

    Lifecycle: ``start()`` schedules the asyncio task; ``stop()``
    cancels it and awaits exit. ``start`` is idempotent (re-calling
    is a no-op); ``stop`` is idempotent (re-calling does nothing).
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        paths: list[str],
        batch_window_ms: int = DEFAULT_BATCH_WINDOW_MS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        on_change: Callable[[list[dict]], Awaitable[None]],
    ) -> None:
        self._root = workspace_root
        self._paths = list(paths)
        self._batch_window_ms = batch_window_ms
        self._poll = poll_interval_seconds
        self._on_change = on_change
        self._task: asyncio.Task | None = None
        self._stopping = False
        # Path → mtime (None if file doesn't exist). Populated on the
        # first stat baseline; subsequent polls diff against this dict.
        self._state: dict[str, float | None] = {}

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name=f"watch-files:{','.join(self._paths[:3])}",
        )

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def _run(self) -> None:
        # Establish baseline before announcing any changes — only
        # mutations AFTER the watcher started are interesting.
        self._state = self._snapshot()
        while not self._stopping:
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break
            try:
                changes = self._diff_against_state()
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "watch-files: stat poll failed: %s", exc,
                )
                continue
            if not changes:
                continue
            # Coalesce — wait for the batch window to elapse, then
            # collect any additional changes that landed in the
            # interim, fire once.
            if self._batch_window_ms > 0:
                try:
                    await asyncio.sleep(self._batch_window_ms / 1000.0)
                except asyncio.CancelledError:
                    break
                extra = self._diff_against_state()
                if extra:
                    # Merge: later events on the same path win.
                    by_path: dict[str, dict] = {c["path"]: c for c in changes}
                    for c in extra:
                        by_path[c["path"]] = c
                    changes = list(by_path.values())
            try:
                await self._on_change(changes)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "watch-files: on_change callback raised: %s", exc,
                )

    def _snapshot(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for rel in self._paths:
            out[rel] = _stat_mtime(self._root / rel)
        return out

    def _diff_against_state(self) -> list[dict]:
        """Return change events for paths whose mtime/existence shifted.

        Mutates ``self._state`` to the new snapshot so subsequent
        polls see the new baseline (i.e. each change is reported
        exactly once).
        """
        changes: list[dict] = []
        for rel in self._paths:
            old = self._state.get(rel)
            new = _stat_mtime(self._root / rel)
            if old == new:
                continue
            self._state[rel] = new
            if old is None and new is not None:
                event_type = "created"
            elif old is not None and new is None:
                event_type = "deleted"
            else:
                event_type = "modified"
            changes.append(
                {
                    "path": rel,
                    "event_type": event_type,
                    "mtime_after": (
                        datetime.fromtimestamp(new, tz=timezone.utc).isoformat()
                        if new is not None
                        else None
                    ),
                }
            )
        return changes


def _stat_mtime(p: Path) -> float | None:
    """Return ``p``'s mtime, or ``None`` if the path doesn't exist.

    Wraps ``Path.stat()`` so FileNotFoundError (and PermissionError —
    typical on Windows when an antivirus has the file open) cleanly
    translate to ``None`` rather than crashing the polling loop.
    """
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError:
        # Permission denied, file in use on Windows, etc. — treat as
        # "unknown state" so the loop doesn't spin on it.
        return None


# ===========================================================================
# WatcherManager
# ===========================================================================


WorkspaceRootResolver = Callable[[str], Awaitable[Path | None]]


class WatcherManager:
    """Lifecycle owner for :class:`LocalWorkspaceWatcher` instances.

    On each scan:

    1. Ask the scheduler for ``watch:*`` parked sessions.
    2. Start watchers for any park we don't already have one for.
    3. Stop watchers for parks that have gone away (resumed,
       cancelled, expired).

    When a watcher fires, its callback publishes the change batch on
    the event bus under the park's ``parked_event_key``. The bus
    listener flips the parked row to resumable; the worker pool
    claims and resumes the turn.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        scheduler,
        workspace_root_resolver: WorkspaceRootResolver,
        scan_interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._bus = bus
        self._scheduler = scheduler
        self._resolve = workspace_root_resolver
        self._scan = scan_interval_seconds
        self._poll = poll_interval_seconds
        self._task: asyncio.Task | None = None
        self._stopping = False
        # event_key → live watcher
        self._watchers: dict[str, LocalWorkspaceWatcher] = {}

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="yield-watcher-manager",
        )

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None:
            self._task = None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for w in list(self._watchers.values()):
            try:
                await w.stop()
            except Exception:  # noqa: BLE001
                logger.exception("watcher.stop failed during manager teardown")
        self._watchers.clear()

    def active_watchers(self) -> set[str]:
        """Set of event_keys whose watchers are currently running."""
        return set(self._watchers.keys())

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._scan_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "yield-watcher-manager: scan failed: %s", exc,
                )
            try:
                await asyncio.sleep(self._scan)
            except asyncio.CancelledError:
                break

    async def _scan_once(self) -> None:
        active_parks = await _find_active_watch_parks(self._scheduler)
        # Start watchers for new parks
        seen_keys: set[str] = set()
        for park in active_parks:
            seen_keys.add(park["event_key"])
            if park["event_key"] in self._watchers:
                continue
            await self._start_watcher(park)
        # Stop watchers for parks that have disappeared / flipped.
        stale = set(self._watchers.keys()) - seen_keys
        for key in stale:
            await self._stop_watcher(key)

    async def _start_watcher(self, park: dict) -> None:
        event_key: str = park["event_key"]
        workspace_id: str | None = park.get("workspace_id")
        if workspace_id is None:
            logger.warning(
                "watcher-manager: skipping park %s (no workspace_id)",
                event_key,
            )
            return
        root = await self._resolve(workspace_id)
        if root is None:
            logger.warning(
                "watcher-manager: workspace %s has no root path; "
                "skipping watch park %s",
                workspace_id, event_key,
            )
            return
        paths = park.get("paths") or []
        if not paths:
            logger.warning(
                "watcher-manager: watch park %s has empty paths; skipping",
                event_key,
            )
            return
        batch_window_ms = int(park.get("batch_window_ms") or DEFAULT_BATCH_WINDOW_MS)

        # Bind event_key into the on_change closure so the publish
        # path doesn't have to reverse-lookup the watcher.
        async def on_change(changes: list[dict], _key=event_key) -> None:
            await self._bus.publish(_key, {"changes": changes})

        watcher = LocalWorkspaceWatcher(
            workspace_root=root,
            paths=paths,
            batch_window_ms=batch_window_ms,
            poll_interval_seconds=self._poll,
            on_change=on_change,
        )
        watcher.start()
        self._watchers[event_key] = watcher
        logger.info(
            "watcher-manager: started watcher for %s (paths=%s, root=%s)",
            event_key, paths, root,
        )

    async def _stop_watcher(self, event_key: str) -> None:
        watcher = self._watchers.pop(event_key, None)
        if watcher is None:
            return
        try:
            await watcher.stop()
        except Exception:  # noqa: BLE001
            logger.exception(
                "watcher-manager: watcher.stop failed for %s", event_key,
            )
        logger.info("watcher-manager: stopped watcher for %s", event_key)


# ===========================================================================
# Scheduler-flavour park lookup
# ===========================================================================


async def _find_active_watch_parks(scheduler) -> list[dict]:
    """Return descriptors for sessions parked on ``watch:*`` keys.

    Each descriptor carries the fields the watcher needs:
    ``event_key``, ``workspace_id``, ``paths``, ``batch_window_ms``.
    Returns parks in ``parked`` state only — once a row flips to
    ``resumable``, the manager drops the watcher (the resume will
    rebuild it if the new worker re-parks).
    """
    from matrix.scheduler.in_memory import InMemoryScheduler
    from matrix.scheduler.postgres import PostgresScheduler

    out: list[dict] = []

    if isinstance(scheduler, InMemoryScheduler):
        async with scheduler._lock:
            for sess in scheduler._sessions.values():
                if (
                    sess.parked_status == "parked"
                    and sess.parked_event_key is not None
                    and sess.parked_event_key.startswith("watch:")
                ):
                    out.append(_extract_park_dict(sess))
        return out

    if isinstance(scheduler, PostgresScheduler):
        sql = """
            SELECT id,
                   data->>'workspace_id'      AS workspace_id,
                   data->>'parked_event_key'  AS event_key,
                   data->'parked_state'        AS parked_state
              FROM sessions
             WHERE data->>'parked_status' = 'parked'
               AND data->>'parked_event_key' LIKE 'watch:%'
             LIMIT 200
        """
        async with scheduler._storage.pool.acquire() as conn:
            rows = await conn.fetch(sql)
        for row in rows:
            blob = row["parked_state"] or {}
            yielded = blob.get("yielded") or {}
            meta = yielded.get("resume_metadata") or {}
            out.append(
                {
                    "event_key": row["event_key"],
                    "workspace_id": row["workspace_id"],
                    "paths": list(meta.get("paths") or []),
                    "batch_window_ms": int(
                        meta.get("batch_window_ms") or DEFAULT_BATCH_WINDOW_MS
                    ),
                }
            )
        return out

    return []


def _extract_park_dict(sess) -> dict:
    """Pull the watch-park descriptor out of an in-memory Session."""
    blob = sess.parked_state or {}
    yielded = blob.get("yielded") or {}
    meta = yielded.get("resume_metadata") or {}
    return {
        "event_key": sess.parked_event_key,
        "workspace_id": sess.workspace_id,
        "paths": list(meta.get("paths") or []),
        "batch_window_ms": int(
            meta.get("batch_window_ms") or DEFAULT_BATCH_WINDOW_MS
        ),
    }


__all__ = [
    "DEFAULT_BATCH_WINDOW_MS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_SCAN_INTERVAL_SECONDS",
    "LocalWorkspaceWatcher",
    "WatcherManager",
    "WorkspaceRootResolver",
]
