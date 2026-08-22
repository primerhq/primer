"""WorkspaceEventBridge: runtime lifecycle frames onto the event log.

Leader-elected (the WatcherManager idiom): every pod runs the
supervisor, one leads. The leader scans Workspace rows whose
``events`` config is enabled, holds one lifecycle stream per opted-in
workspace, and emits ``workspace.file_changed`` /
``workspace.exec_started`` / ``workspace.exec_exited`` onto the
platform event log, scoped by ``workspace_id``.

Stream resolution is injected (``stream_resolver``) so the bridge
stays transport-agnostic: sandbox workspaces stream over the runtime
websocket's EVENTS_SUBSCRIBE op (file + exec kinds); local
workspaces fall back to a host inotify probe (file events only).
Streams reconnect with backoff on failure and are closed when the
workspace opts out or disappears.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from primer.bus.scheduler_tasks import _BackgroundTask
from primer.events.recorder import recorder_for
from primer.int.coordinator import ROLE_WORKSPACE_EVENTS
from primer.model.storage import OffsetPage
from primer.model.workspace import Workspace, WorkspaceEventsConfig

logger = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL_SECONDS = 15.0
RECONNECT_DELAY_SECONDS = 5.0

WORKSPACE_EVENT_KINDS = frozenset({
    "file_changed", "exec_started", "exec_exited",
})

# workspace_id, config -> async iterator of {"kind": ..., ...} dicts,
# or None when the workspace has no live stream source right now.
StreamResolver = Callable[
    [str, WorkspaceEventsConfig],
    Awaitable[AsyncIterator[dict[str, Any]] | None],
]


class WorkspaceEventBridge(_BackgroundTask):
    role = ROLE_WORKSPACE_EVENTS

    def __init__(
        self,
        *,
        storage_provider: Any,
        stream_resolver: StreamResolver,
        event_bus: Any = None,
        scan_interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(name="workspace-event-bridge")
        self._sp = storage_provider
        self._resolve = stream_resolver
        self._bus = event_bus
        self._scan = scan_interval_seconds
        self._streams: dict[str, asyncio.Task] = {}

    async def _run(self) -> None:
        try:
            while not self._stopping:
                try:
                    await self._scan_once()
                except Exception:  # noqa: BLE001 - the loop must survive
                    logger.exception("workspace-event-bridge: scan failed")
                try:
                    await asyncio.sleep(self._scan)
                except asyncio.CancelledError:
                    break
        finally:
            for task in self._streams.values():
                task.cancel()
            for task in self._streams.values():
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            self._streams.clear()

    async def _scan_once(self) -> None:
        """Reconcile held streams with the opted-in workspace set."""
        page = await self._sp.get_storage(Workspace).list(
            OffsetPage(length=200),
        )
        wanted: dict[str, WorkspaceEventsConfig] = {
            row.id: row.events
            for row in page.items
            if row.events is not None and row.events.enabled
        }
        for workspace_id in list(self._streams):
            if workspace_id not in wanted:
                self._streams.pop(workspace_id).cancel()
                logger.info(
                    "workspace-event-bridge: stopped stream for %s",
                    workspace_id,
                )
        for workspace_id, config in wanted.items():
            task = self._streams.get(workspace_id)
            if task is None or task.done():
                self._streams[workspace_id] = asyncio.create_task(
                    self._stream_loop(workspace_id, config),
                    name=f"ws-events:{workspace_id}",
                )

    async def _stream_loop(
        self, workspace_id: str, config: WorkspaceEventsConfig,
    ) -> None:
        """Hold one lifecycle stream; reconnect with backoff until
        cancelled (the scan loop cancels on opt-out/disappearance)."""
        recorder = recorder_for(self._sp, self._bus)
        while True:
            try:
                stream = await self._resolve(workspace_id, config)
                if stream is None:
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                    continue
                async for item in stream:
                    kind = (item or {}).get("kind")
                    if kind not in WORKSPACE_EVENT_KINDS:
                        continue
                    if kind not in config.kinds:
                        continue
                    payload = {
                        k: v for k, v in item.items() if k != "kind"
                    }
                    # Literal per kind so the action-site pinning scan
                    # sees every type it guards.
                    if kind == "file_changed":
                        await recorder.emit(
                            "workspace.file_changed",
                            workspace_id=workspace_id,
                            payload=payload,
                        )
                    elif kind == "exec_started":
                        await recorder.emit(
                            "workspace.exec_started",
                            workspace_id=workspace_id,
                            payload=payload,
                        )
                    else:
                        await recorder.emit(
                            "workspace.exec_exited",
                            workspace_id=workspace_id,
                            payload=payload,
                        )
                # Stream ended cleanly (workspace restarted, runtime
                # redeployed): reconnect after the delay.
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - reconnect with backoff
                logger.warning(
                    "workspace-event-bridge: stream for %s failed; "
                    "reconnecting", workspace_id, exc_info=True,
                )
                try:
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                except asyncio.CancelledError:
                    return


__all__ = ["WorkspaceEventBridge", "WORKSPACE_EVENT_KINDS"]
