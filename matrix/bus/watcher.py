"""File-watcher backend for the ``watch_files`` yielding tool.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md`` §8.3.

Three public classes + one protocol:

* :class:`StatProbe` — protocol describing how to snapshot a list of
  paths. Two implementations are provided:
  :class:`HostStatProbe` (host-side ``os.stat``) and
  :class:`SandboxStatProbe` (exec-based, for container / k8s workspaces).
* :class:`WorkspaceFilesWatcher` — the unit. Polls via a ``StatProbe``
  for a list of workspace-relative paths, fires an async ``on_change``
  callback with a coalesced batch of change events whenever something
  shifts. No bus, no scheduler — pure observation. Trivially
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
:class:`WorkspaceFilesWatcher` surface (constructor + start/stop +
on_change callback) doesn't change, only its insides.

Container / k8s workspaces use a different probe backend: instead of
host-side os.stat, they exec ``stat -c '%n|%Y|%s'`` inside the sandbox.
One exec call per poll cycle batches all of a watcher's paths to
amortise the docker-exec overhead (~100ms per call). Operators
watching more than ~50 paths per session will see noticeable polling
latency; the host-side variant has no such ceiling.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from matrix.bus.scheduler_tasks import _BackgroundTask
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

# Maximum paths per single sandbox exec() call to stay under ARG_MAX.
# A bash -c script string is bounded by ARG_MAX (typically 128KB).
# With paths of up to ~256 bytes each, 50 paths * 256 = ~12KB, well
# within limit. Paths over 50 are split into multiple exec calls.
_SANDBOX_BATCH_SIZE = 50


# ===========================================================================
# StatProbe protocol
# ===========================================================================


@runtime_checkable
class StatProbe(Protocol):
    """Snapshot the (mtime, size, exists) state of a list of paths.

    Per call, returns a dict keyed by the path string (as it was
    passed in). Each value is a (mtime_seconds_or_None, size_or_None,
    exists_bool) triple. mtime is integer seconds since the epoch
    (best portability — sandbox stat -c '%Y' returns seconds); size
    is bytes; exists is True iff the path resolved to something on
    the filesystem. Non-existent paths report (None, None, False) —
    they're a valid state, not an error, and may exist in a later
    poll.
    """

    async def snapshot(
        self, paths: list[str],
    ) -> dict[str, tuple[int | float | None, int | None, bool]]:
        ...


# ===========================================================================
# HostStatProbe
# ===========================================================================


class HostStatProbe:
    """StatProbe that calls os.stat() on host filesystem paths.

    Each path in ``paths`` is resolved as ``root / path``. ENOENT →
    (None, None, False). Other OSError → (None, None, False) with a
    warning logged (permission denied etc. are 'no event' from the
    agent's view).

    Returns float mtime (sub-second precision from st_mtime_ns) to
    correctly detect rapid back-to-back writes within the same wall-clock
    second. The SandboxStatProbe returns integer seconds (stat -c %Y
    only provides second resolution), so both are acceptable by the
    callers which only care about change detection, not the exact value.
    """

    def __init__(self, *, root: Path) -> None:
        self._root = root

    async def snapshot(
        self, paths: list[str],
    ) -> dict[str, tuple[int | float | None, int | None, bool]]:
        out: dict[str, tuple[int | None, int | None, bool]] = {}
        for rel in paths:
            out[rel] = _host_stat(self._root / rel)
        return out


def _host_stat(p: Path) -> tuple[float | None, int | None, bool]:
    """Return (mtime_float, size, exists) for a host path.

    Uses ``st_mtime`` (float, nanosecond precision on most modern kernels)
    rather than truncating to integer seconds so that two rapid writes
    within the same wall-clock second are still distinguishable.
    """
    try:
        st = p.stat()
        return (st.st_mtime, st.st_size, True)
    except FileNotFoundError:
        return (None, None, False)
    except OSError as exc:
        logger.warning(
            "watch-files: os.stat(%s) failed: %s — treating as missing",
            p, exc,
        )
        return (None, None, False)


# ===========================================================================
# SandboxStatProbe
# ===========================================================================


class SandboxStatProbe:
    """StatProbe that execs ``stat`` inside a :class:`~matrix.int.sandbox.Sandbox`.

    All paths are workspace-relative (e.g. ``"src/main.py"``).
    They are joined with ``workspace_root`` before being passed to the
    sandbox script (e.g. ``"/workspace/src/main.py"``).

    Paths containing newlines are rejected at construction time with
    :class:`ValueError` — the output parser splits on ``\\n``.

    Each :meth:`snapshot` call batches up to ``_SANDBOX_BATCH_SIZE``
    paths into one exec call to amortise the docker-exec overhead.
    """

    def __init__(self, *, sandbox: "Sandbox", workspace_root: str) -> None:
        self._sandbox = sandbox
        self._workspace_root = workspace_root.rstrip("/")

    def _abs(self, rel: str) -> str:
        return f"{self._workspace_root}/{rel}"

    async def snapshot(
        self, paths: list[str],
    ) -> dict[str, tuple[int | float | None, int | None, bool]]:
        # Reject newlines early — they would break our line-oriented parsing.
        for rel in paths:
            if "\n" in rel:
                raise ValueError(
                    f"SandboxStatProbe: path contains a newline: {rel!r}"
                )

        out: dict[str, tuple[int | None, int | None, bool]] = {}
        # Seed all as missing; successful parse rows will overwrite.
        for rel in paths:
            out[rel] = (None, None, False)

        # Process in batches to avoid ARG_MAX overflow.
        # See module-level _SANDBOX_BATCH_SIZE comment.
        for i in range(0, len(paths), _SANDBOX_BATCH_SIZE):
            batch = paths[i : i + _SANDBOX_BATCH_SIZE]
            batch_result = await self._exec_batch(batch)
            out.update(batch_result)

        return out

    async def _exec_batch(
        self, paths: list[str],
    ) -> dict[str, tuple[int | float | None, int | None, bool]]:
        """Run a single stat exec for the given relative paths."""
        out: dict[str, tuple[int | float | None, int | None, bool]] = {}

        # Build a shell script that, for each path, either prints
        # <rel_path>|<mtime_sec>|<size_bytes> or <rel_path>|MISS|MISS.
        # We iterate over the workspace-absolute paths in the script;
        # the output uses the relative path so we can match back to
        # the caller's keys without stripping the workspace_root prefix.
        parts: list[str] = []
        rel_to_abs: dict[str, str] = {}
        for rel in paths:
            abs_p = self._abs(rel)
            rel_to_abs[rel] = abs_p
            # shlex.quote ensures no shell injection from the path string.
            q = shlex.quote(abs_p)
            rel_q = shlex.quote(rel)
            parts.append(
                f"if [ -e {q} ]; then "
                f"stat -c {rel_q}'|%Y|%s' {q}; "
                f"else echo {rel_q}'|MISS|MISS'; fi"
            )
        script = "; ".join(parts)

        try:
            result = await self._sandbox.exec(
                ["sh", "-c", script],
                workdir=self._workspace_root,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SandboxStatProbe: sandbox.exec raised: %s — "
                "treating all paths as missing",
                exc,
            )
            for rel in paths:
                out[rel] = (None, None, False)
            return out

        if result.exit_code != 0:
            logger.debug(
                "SandboxStatProbe: exec exited %d (stderr=%r) — "
                "treating all paths as missing",
                result.exit_code, result.stderr[:200],
            )
            for rel in paths:
                out[rel] = (None, None, False)
            return out

        # Parse output: each line is <rel_path>|<mtime>|<size>  or  <rel_path>|MISS|MISS
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts_row = line.split("|", 2)
            if len(parts_row) != 3:  # noqa: PLR2004
                logger.warning(
                    "SandboxStatProbe: malformed stat output line %r — skipping",
                    line,
                )
                continue
            rel_path, mtime_raw, size_raw = parts_row
            if mtime_raw == "MISS" or size_raw == "MISS":
                out[rel_path] = (None, None, False)
            else:
                try:
                    out[rel_path] = (int(mtime_raw), int(size_raw), True)
                except ValueError:
                    logger.warning(
                        "SandboxStatProbe: could not parse stat values "
                        "mtime=%r size=%r for path %r — skipping",
                        mtime_raw, size_raw, rel_path,
                    )

        return out


# ===========================================================================
# WorkspaceFilesWatcher
# ===========================================================================


class WorkspaceFilesWatcher:
    """Polling watcher for a fixed list of workspace-relative paths.

    Backend-agnostic: works against any object that implements the
    StatProbe protocol. The hot loop calls probe.snapshot(paths)
    every poll_interval_seconds, diffs against the previous
    snapshot, and emits a coalesced batch via on_change.

    Each path is snapshotted every ``poll_interval_seconds``. When a
    change is detected (mtime delta, file appeared, or file
    disappeared), the watcher waits ``batch_window_ms`` for more
    changes, collects them, and fires ``on_change`` once with the
    full batch.

    A burst that crosses the batch window boundary is split — the
    watcher emits whatever it had at window-close, then re-arms the
    detection state and waits for the next change.

    Lifecycle: ``start()`` schedules the asyncio task; ``stop()``
    cancels it and awaits exit. ``start`` is idempotent (re-calling
    is a no-op); ``stop`` is idempotent (re-calling does nothing).
    """

    def __init__(
        self,
        *,
        probe: StatProbe,
        paths: list[str],
        on_change: Callable[[list[dict]], Awaitable[None]],
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        batch_window_ms: int = DEFAULT_BATCH_WINDOW_MS,
    ) -> None:
        self._probe = probe
        self._paths = list(paths)
        self._batch_window_ms = batch_window_ms
        self._poll = poll_interval_seconds
        self._on_change = on_change
        self._task: asyncio.Task | None = None
        self._stopping = False
        # Path → (mtime, size, exists). Populated on the first snapshot
        # baseline; subsequent polls diff against this dict.
        self._state: dict[str, tuple[int | float | None, int | None, bool]] = {}

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
        self._state = await self._probe.snapshot(self._paths)
        while not self._stopping:
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break
            try:
                changes = await self._diff_against_state()
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
                try:
                    extra = await self._diff_against_state()
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "watch-files: stat poll (batch window) failed: %s", exc,
                    )
                    extra = []
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

    async def _diff_against_state(self) -> list[dict]:
        """Return change events for paths whose mtime/existence shifted.

        Mutates ``self._state`` to the new snapshot so subsequent
        polls see the new baseline (i.e. each change is reported
        exactly once).
        """
        new_snap = await self._probe.snapshot(self._paths)
        changes: list[dict] = []
        for rel in self._paths:
            old = self._state.get(rel)
            new = new_snap.get(rel, (None, None, False))
            old_mtime = old[0] if old is not None else None
            old_exists = old[2] if old is not None else False
            new_mtime = new[0]
            new_exists = new[2]
            # Determine if anything changed: either existence or mtime
            changed = (old_exists != new_exists) or (old_mtime != new_mtime)
            if not changed:
                continue
            self._state[rel] = new
            if not old_exists and new_exists:
                event_type = "created"
            elif old_exists and not new_exists:
                event_type = "deleted"
            else:
                event_type = "modified"
            changes.append(
                {
                    "path": rel,
                    "event_type": event_type,
                    "mtime_after": (
                        datetime.fromtimestamp(new_mtime, tz=timezone.utc).isoformat()
                        if new_mtime is not None
                        else None
                    ),
                }
            )
        return changes


# ===========================================================================
# WatcherManager
# ===========================================================================


WorkspaceProbeResolver = Callable[[str], Awaitable["StatProbe | None"]]

# Keep the old name available for any external code that may reference it.
WorkspaceRootResolver = WorkspaceProbeResolver


class WatcherManager(_BackgroundTask):
    """Lifecycle owner for :class:`WorkspaceFilesWatcher` instances.

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
        self._poll = poll_interval_seconds
        # event_key → live watcher
        self._watchers: dict[str, WorkspaceFilesWatcher] = {}

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

        watcher = WorkspaceFilesWatcher(
            probe=probe,
            paths=paths,
            batch_window_ms=batch_window_ms,
            poll_interval_seconds=self._poll,
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
    "HostStatProbe",
    "SandboxStatProbe",
    "StatProbe",
    "WorkspaceFilesWatcher",
    "WatcherManager",
    "WorkspaceProbeResolver",
    "WorkspaceRootResolver",
]
