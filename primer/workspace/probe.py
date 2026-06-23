"""Workspace health-probe task.

Lives in the API process, ticks every ~30s, pings each ``running`` /
``failed`` workspace's runtime, and flips ``phase`` on three-strike
misses (``running`` -> ``failed``) or three-strike hits while failed
(``failed`` -> ``running``). Writes results back to the persisted
:class:`primer.model.workspace.Workspace` row via the storage provider.

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

from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace_session import SessionStatus, WorkspaceSession


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
    ) -> None:
        self._sp = storage_provider
        self._registry = registry
        self._interval = interval_seconds
        self._miss_counts: dict[str, int] = defaultdict(int)
        self._hit_counts: dict[str, int] = defaultdict(int)
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Run the probe loop until :meth:`stop` is called."""
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

        offset = 0
        while True:
            page = await storage.list(
                OffsetPage(offset=offset, length=_LIST_PAGE_SIZE)
            )
            items = list(page.items)
            for ws in items:
                if ws.phase not in ("pending", "running", "failed"):
                    continue
                await self._probe_one(storage, ws)
            if len(items) < _LIST_PAGE_SIZE:
                break
            offset += _LIST_PAGE_SIZE

    async def _probe_one(self, storage: Any, ws: Any) -> None:
        """Ping one workspace and update its row with the result."""
        ok = False
        fail_reason: str | None = None
        try:
            handle = await self._registry.get_workspace(ws.id)
            ok = bool(await handle.ping())
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
            await self._reconcile_sessions_for_failed_workspace(ws.id)

    async def _reconcile_sessions_for_failed_workspace(
        self, workspace_id: str
    ) -> None:
        """Mark every non-ENDED session on a failed workspace as
        ENDED/`workspace_lost` so the UI doesn't surface immortal rows."""
        try:
            session_storage = self._sp.get_storage(WorkspaceSession)
        except Exception:  # noqa: BLE001 -- storage layer unavailable
            logger.warning(
                "workspace probe: session storage unavailable, "
                "cannot reconcile %s", workspace_id,
            )
            return

        try:
            page = await session_storage.find(
                Predicate(
                    left=FieldRef(name="workspace_id"),
                    op=Op.EQ,
                    right=Value(value=workspace_id),
                ),
                OffsetPage(offset=0, length=_LIST_PAGE_SIZE),
            )
        except Exception:  # noqa: BLE001 -- find unavailable
            logger.exception(
                "workspace probe: failed to query sessions on %s",
                workspace_id,
            )
            return

        now = datetime.now(timezone.utc)
        reconciled = 0
        for sess in page.items:
            if sess.status == SessionStatus.ENDED:
                continue
            updated_sess = sess.model_copy(update={
                "status": SessionStatus.ENDED,
                "ended_reason": "workspace_lost",
                "ended_at": now,
            })
            try:
                await session_storage.update(updated_sess)
            except Exception:  # noqa: BLE001 -- log + continue
                logger.exception(
                    "workspace probe: failed to reconcile session %s on %s",
                    sess.id, workspace_id,
                )
                continue
            reconciled += 1

        if reconciled:
            logger.info(
                "workspace probe: reconciled %d session(s) on failed "
                "workspace %s as ENDED/workspace_lost",
                reconciled, workspace_id,
            )


__all__ = ["WorkspaceProbeTask"]
