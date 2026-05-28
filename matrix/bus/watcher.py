"""File-watcher backend for the ``watch_files`` yielding tool.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md`` §8.3.

Two public classes + one type alias:

* :class:`EventDrivenWatcher` — the unit.  Consumes push events from a
  :class:`~matrix.bus.ws_watch_probe.WatchProbe` and fires an async
  ``on_change`` callback with a coalesced batch of change events whenever
  something arrives.  No bus, no scheduler — pure observation.
* :class:`WatcherManager` — the lifecycle owner.  Periodically scans the
  scheduler for sessions parked on ``watch:*`` keys, starts a watcher per
  park, and stops watchers when the park flips to ``resumable`` or the
  deadline passes.  Publishes change bursts on the event bus on behalf of
  each watcher.

Watch probes are resolved via a :data:`WorkspaceProbeResolver` callable that
maps ``workspace_id → WatchProbe | None``.  For local workspaces this
returns a :class:`~matrix.bus.host_inotify_probe.HostInotifyProbe`; for
container / k8s workspaces it returns a
:class:`~matrix.bus.ws_watch_probe.WSWatchProbe`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from matrix.bus.scheduler_tasks import _BackgroundTask
from matrix.bus.ws_watch_probe import Change, WatchProbe
from matrix.int.coordinator import ROLE_WATCHER_MANAGER
from matrix.int.event_bus import EventBus

if TYPE_CHECKING:
    from matrix.int.sandbox import Sandbox
    from matrix.scheduler.in_memory import InMemoryScheduler
    from matrix.scheduler.postgres import PostgresScheduler


logger = logging.getLogger(__name__)


DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_SCAN_INTERVAL_SECONDS = 2.0
DEFAULT_BATCH_WINDOW_MS = 250

# Event type normalisation: new probes use present-tense verbs;
# the bus payload uses past tense for backward compatibility.
_EVENT_TYPE_MAP: dict[str, str] = {
    "modify": "modified",
    "create": "created",
    "delete": "deleted",
    # Pass through already-normalised values unchanged.
    "modified": "modified",
    "created": "created",
    "deleted": "deleted",
}


def _normalise_event_type(event_type: str) -> str:
    return _EVENT_TYPE_MAP.get(event_type, event_type)


def _change_to_dict(change: Change) -> dict:
    """Convert a :class:`~matrix.bus.ws_watch_probe.Change` to the bus payload dict."""
    return {
        "path": change.path,
        "event_type": _normalise_event_type(change.event_type),
        "mtime_after": (
            datetime.fromtimestamp(change.mtime, tz=timezone.utc).isoformat()
            if change.mtime is not None
            else None
        ),
    }


# ===========================================================================
# EventDrivenWatcher
# ===========================================================================


class EventDrivenWatcher:
    """Event-driven watcher for a fixed list of workspace-relative paths.

    Backend-agnostic: works against any object that implements the
    :class:`~matrix.bus.ws_watch_probe.WatchProbe` interface.

    The watcher opens a ``probe.watch(paths)`` async iterator and forwards
    arriving :class:`~matrix.bus.ws_watch_probe.Change` events to the
    ``on_change`` callback after an optional coalescing window.

    Coalescing: when the first change arrives, the watcher waits
    ``batch_window_ms`` milliseconds, collects any additional changes that
    land in the interim, then fires ``on_change`` once with the full batch.
    Later changes on the same path within the window overwrite earlier ones
    (last-writer wins).

    Lifecycle: ``start()`` schedules the asyncio task; ``stop()`` cancels it
    and awaits exit.  ``start`` is idempotent; ``stop`` is idempotent.
    """

    def __init__(
        self,
        *,
        probe: WatchProbe,
        paths: list[str],
        on_change: Callable[[list[dict]], Awaitable[None]],
        batch_window_ms: int = DEFAULT_BATCH_WINDOW_MS,
    ) -> None:
        self._probe = probe
        self._paths = list(paths)
        self._batch_window_ms = batch_window_ms
        self._on_change = on_change
        self._task: asyncio.Task | None = None
        self._stopping = False

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
        # Accumulated pending changes: path → dict (last-writer wins).
        pending: dict[str, dict] = {}
        flush_task: asyncio.Task | None = None

        async def _flush_after_window() -> None:
            try:
                await asyncio.sleep(self._batch_window_ms / 1000.0)
            except asyncio.CancelledError:
                return
            if pending:
                batch = list(pending.values())
                pending.clear()
                try:
                    await self._on_change(batch)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("watch-files: on_change callback raised: %s", exc)

        try:
            async for change in self._probe.watch(self._paths):
                if self._stopping:
                    break
                d = _change_to_dict(change)
                pending[change.path] = d
                # Arm the flush timer once (the first change in a burst).
                if flush_task is None or flush_task.done():
                    flush_task = asyncio.create_task(_flush_after_window())
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("watch-files: probe.watch raised: %s", exc)
        finally:
            # Drain any remaining flush timer.
            if flush_task is not None and not flush_task.done():
                flush_task.cancel()
                try:
                    await flush_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


# ===========================================================================
# WatcherManager
# ===========================================================================


WorkspaceProbeResolver = Callable[[str], Awaitable["WatchProbe | None"]]

# Keep the old name available for any external code that may reference it.
WorkspaceRootResolver = WorkspaceProbeResolver


class WatcherManager(_BackgroundTask):
    """Lifecycle owner for :class:`EventDrivenWatcher` instances.

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

    role = ROLE_WATCHER_MANAGER

    def __init__(
        self,
        *,
        bus: EventBus,
        scheduler,
        workspace_root_resolver: WorkspaceProbeResolver,
        scan_interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(name="yield-watcher-manager")
        self._bus = bus
        self._scheduler = scheduler
        self._resolve = workspace_root_resolver
        self._scan = scan_interval_seconds
        # poll_interval_seconds is kept for API compatibility but no longer
        # used internally — the new probes are push-based.
        self._poll = poll_interval_seconds
        # event_key → live watcher
        self._watchers: dict[str, EventDrivenWatcher] = {}

    async def stop(self) -> None:
        await super().stop()
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
        probe = await self._resolve(workspace_id)
        if probe is None:
            logger.warning(
                "watcher-manager: workspace %s has no probe; "
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

        watcher = EventDrivenWatcher(
            probe=probe,
            paths=paths,
            batch_window_ms=batch_window_ms,
            on_change=on_change,
        )
        watcher.start()
        self._watchers[event_key] = watcher
        logger.info(
            "watcher-manager: started watcher for %s (paths=%s)",
            event_key, paths,
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
            # asyncpg returns JSONB as a string unless a codec is
            # registered on the connection — parse defensively.
            raw_blob = row["parked_state"]
            if isinstance(raw_blob, str):
                import json as _json
                raw_blob = _json.loads(raw_blob)
            blob = raw_blob or {}
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
    "EventDrivenWatcher",
    "WatcherManager",
    "WorkspaceProbeResolver",
    "WorkspaceRootResolver",
]
