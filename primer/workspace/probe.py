"""Workspace health-probe task.

Lives in the API process, ticks every ~30s, pings each ``running`` /
``failed`` workspace's runtime, and flips ``phase`` on three-strike
misses (``running`` -> ``failed``) or three-strike hits while failed
(``failed`` -> ``running``). Writes results back to the persisted
:class:`primer.model.workspace.Workspace` row via the storage provider.
Waits one interval before its first tick (see ``start_delay_seconds``) so a
freshly-started process doesn't count its own boot-time settling as misses.

Owned by the API lifespan; uses the :class:`WorkspaceRegistry` to resolve
live workspace handles. The registry stays a pure cache — the probe
owns the per-id streak counters here so the registry doesn't need to
track health.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from primer.model.storage import OffsetPage
from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace import WorkspaceProvider, WorkspaceProviderType
from primer.model.workspace_refusal import WorkspaceRefusedError
from primer.workspace.session_reconcile import reconcile_sessions_to_workspace_lost


if TYPE_CHECKING:
    from primer.api.registries.workspace_registry import WorkspaceRegistry
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


_FAILS_BEFORE_FAILED = 3
_HITS_BEFORE_RUNNING = 3
_LIST_PAGE_SIZE = 200


class WorkspaceProbeTask:
    """Background loop that drives workspace ``phase`` transitions.

    Wired up by the API lifespan handler. Construct with a
    :class:`StorageProvider` (to read/write :class:`Workspace` rows) and
    a :class:`WorkspaceRegistry` (to resolve live workspace handles for
    ``ping``). Call :meth:`start` to run the loop and :meth:`stop` to
    request shutdown — :meth:`start` returns once the loop observes the
    stop flag.
    """

    def __init__(
        self,
        *,
        storage_provider: "StorageProvider",
        registry: "WorkspaceRegistry",
        interval_seconds: float = 30.0,
        start_delay_seconds: float | None = None,
    ) -> None:
        self._sp = storage_provider
        self._registry = registry
        self._interval = interval_seconds
        # A freshly-started process (e.g. the replacement pod in a rolling
        # deploy) begins every workspace at a clean 0-miss streak (the
        # counters below are in-process, not persisted) and would otherwise
        # start ticking immediately — racing ahead of the app's own startup
        # (session recovery, registries settling) and the workspace
        # runtime's own readiness. A boot-time grace delay before the first
        # tick keeps that race from being counted as real misses (01a0533c,
        # live SEV: a rollout's booting pod struck out its own healthy
        # workspace before the app had finished settling). Defaults to one
        # full interval.
        self._start_delay = (
            interval_seconds if start_delay_seconds is None else start_delay_seconds
        )
        self._miss_counts: dict[str, int] = defaultdict(int)
        self._hit_counts: dict[str, int] = defaultdict(int)
        self._stop_event = asyncio.Event()
        # How many workspace rows (any phase) sit on a local provider, as of the last tick; None until the first
        # one. /v1/health reads in-process state only, so the count is kept here, where the database is already
        # read on a timer, instead of being fetched per request (ticket 01a1072f).
        self.local_workspace_count: int | None = None

    async def start(self) -> None:
        """Run the probe loop until :meth:`stop` is called.

        Waits ``start_delay_seconds`` before the first tick — see the
        docstring on ``__init__`` for why.
        """
        if self._start_delay > 0:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._start_delay
                )
            except TimeoutError:
                pass
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 -- never break the loop
                logger.exception("workspace probe tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        """Signal the probe loop to exit at its next checkpoint."""
        self._stop_event.set()

    async def tick(self) -> None:
        """One probe pass: ping every pending / running / failed workspace.

        Iterates pages of :class:`Workspace` rows, skips terminating
        rows (which are being destroyed), pings the rest via the
        registry, updates streak counters, and writes the new state
        back to storage.
        """
        storage = self._sp.get_storage(WorkspaceRow)
        local_provider_ids = await self._local_provider_ids()
        local_count = 0

        offset = 0
        while True:
            page = await storage.list(
                OffsetPage(offset=offset, length=_LIST_PAGE_SIZE)
            )
            items = list(page.items)
            for ws in items:
                if local_provider_ids is not None and ws.provider_id in local_provider_ids:
                    local_count += 1
                if ws.phase not in ("pending", "running", "failed"):
                    continue
                await self._probe_one(storage, ws)
            if len(items) < _LIST_PAGE_SIZE:
                break
            offset += _LIST_PAGE_SIZE
        if local_provider_ids is not None:
            self.local_workspace_count = local_count

    async def _local_provider_ids(self) -> set[str] | None:
        """Ids of the providers of kind ``local``, or None when they could not be listed (the census keeps its last
        value then; a failed listing must never break the probe's real job)."""
        try:
            ids: set[str] = set()
            offset = 0
            while True:
                page = await self._sp.get_storage(WorkspaceProvider).list(
                    OffsetPage(offset=offset, length=_LIST_PAGE_SIZE)
                )
                items = list(page.items)
                ids.update(p.id for p in items if p.provider == WorkspaceProviderType.LOCAL)
                if len(items) < _LIST_PAGE_SIZE:
                    return ids
                offset += _LIST_PAGE_SIZE
        except Exception:  # noqa: BLE001 -- best-effort census
            logger.warning("workspace probe: could not list the workspace providers for the census", exc_info=True)
            return None

    async def _probe_one(self, storage: Any, ws: Any) -> None:
        """Ping one workspace and update its row with the result."""
        ok = False
        fail_reason: str | None = None
        try:
            handle = await self._registry.get_workspace(ws.id)
            ok = bool(await handle.ping())
        except WorkspaceRefusedError:
            # The deployment refuses this workspace's provider (a local one where the topology forbids it,
            # ticket 01a1072f). The workspace is intact, so this is neither a hit nor a miss: counting it would
            # flip the row to failed and end every open session on it workspace_lost, which is permanent.
            # Leave the row and its streaks exactly as they are.
            return
        except Exception as exc:  # noqa: BLE001 -- treat as a miss
            fail_reason = f"{type(exc).__name__}: {exc}"

        now = datetime.now(timezone.utc)
        updates: dict[str, Any] = {"last_probe_at": now, "last_probe_ok": ok}

        if ws.phase == "running":
            if ok:
                self._miss_counts.pop(ws.id, None)
            else:
                self._miss_counts[ws.id] += 1
                if self._miss_counts[ws.id] >= _FAILS_BEFORE_FAILED:
                    updates["phase"] = "failed"
                    updates["failure_reason"] = (
                        fail_reason or "runtime unreachable"
                    )
                    self._miss_counts.pop(ws.id, None)
        elif ws.phase == "pending":
            # A freshly-created workspace that the create handler didn't
            # mark "running" (e.g. an upgrade from an older row, or a
            # row created via a path that bypassed the handler). One
            # successful ping is enough to promote — the workspace is
            # already materialised, we just hadn't observed that fact.
            if ok:
                updates["phase"] = "running"
                updates["failure_reason"] = None
        elif ws.phase == "failed":
            if ok:
                self._hit_counts[ws.id] += 1
                if self._hit_counts[ws.id] >= _HITS_BEFORE_RUNNING:
                    updates["phase"] = "running"
                    updates["failure_reason"] = None
                    self._hit_counts.pop(ws.id, None)
            else:
                self._hit_counts.pop(ws.id, None)

        updated = ws.model_copy(update=updates)
        try:
            await storage.update(updated)
        except Exception:  # noqa: BLE001 -- log and continue
            logger.exception(
                "workspace probe: failed to persist update for %s", ws.id
            )
            return

        # When the workspace transitions to failed, reconcile any
        # session row still pointing at it — without this sweep, RUNNING
        # / CREATED / WAITING / PAUSED sessions on a dead workspace are
        # orphaned forever (worker can never re-attach to the runtime,
        # so the row never reaches ENDED on its own).
        if updates.get("phase") == "failed":
            await reconcile_sessions_to_workspace_lost(self._sp, ws.id)


__all__ = ["WorkspaceProbeTask"]
