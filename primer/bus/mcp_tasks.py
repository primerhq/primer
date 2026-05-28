"""Background bridge for MCP-task yields.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md`` §8.4.

The :class:`McpTaskBridge` polls the scheduler for sessions parked on
``mcp_task:*`` event keys, then for each one polls the upstream MCP
server's task status. When a task reaches a terminal state
(``completed`` / ``failed`` / ``cancelled``), the bridge fetches the
result and publishes it on the event bus. The bus listener flips the
parked row → resumable; the worker pool claims and the resume hook
turns the published payload into a :class:`ToolCallResult`.

Why polling rather than push?

* MCP's push notifications (``notifications/tasks/status``) require a
  server-side subscription that not all MCP servers implement.
* Polling is uniform across stdio + HTTP transports without extra
  wiring per server.
* For task-style tools (long-running by definition) the polling
  overhead is negligible — default cadence is 5s.

Push support can be added later as a fast-path: when a server's
``status`` notification fires, publish immediately and let the next
poll skip the now-resumable row.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from primer.bus.scheduler_tasks import _BackgroundTask
from primer.int.coordinator import ROLE_MCP_BRIDGE
from primer.int.event_bus import EventBus
from primer.toolset.mcp import McpToolsetProvider

if TYPE_CHECKING:
    from primer.scheduler.in_memory import InMemoryScheduler
    from primer.scheduler.postgres import PostgresScheduler


logger = logging.getLogger(__name__)


DEFAULT_POLL_SECONDS = 5.0
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


class McpTaskBridge(_BackgroundTask):
    """Periodically polls parked MCP-task sessions and publishes results.

    One instance per app suffices. The bridge is provider-registry-
    aware so it can dispatch each park to the right MCP provider:
    ``mcp_task:{toolset_id}:{task_id}`` carries the toolset id in the
    middle segment.
    """

    role = ROLE_MCP_BRIDGE

    def __init__(
        self,
        *,
        bus: EventBus,
        scheduler,
        provider_registry,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        super().__init__(name="yield-mcp-task-bridge")
        self._bus = bus
        self._scheduler = scheduler
        self._registry = provider_registry
        self._poll = poll_seconds

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "mcp-task-bridge: tick failed: %s", exc,
                )
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        """One iteration: find parked mcp_task sessions, poll, publish."""
        parks = await _find_parked_mcp_task_keys(self._scheduler)
        for park in parks:
            try:
                await self._handle_one_park(park)
            except Exception as exc:  # noqa: BLE001
                # Per-park failures don't block other parks — log and
                # continue. A flaky MCP server doesn't break the
                # rest of the system.
                logger.warning(
                    "mcp-task-bridge: poll failed for %s: %s",
                    park.get("event_key"), exc,
                )

    async def _handle_one_park(self, park: dict) -> None:
        toolset_id: str | None = park.get("toolset_id")
        task_id: str | None = park.get("task_id")
        event_key: str | None = park.get("event_key")
        if not (toolset_id and task_id and event_key):
            # Malformed metadata — skip and let the timeout sweeper
            # clean up eventually.
            return
        try:
            provider = await self._registry.get_toolset(toolset_id)
        except Exception:
            # Toolset row gone (renamed / deleted). The session will
            # time out via the sweeper.
            return
        if not isinstance(provider, McpToolsetProvider):
            # Toolset id collision — someone else's toolset, not ours.
            return
        status_result = await provider.poll_task_status(task_id)
        status = (status_result.status or "").lower()
        if status not in TERMINAL_TASK_STATUSES:
            return  # still working, try again next tick

        if status == "cancelled":
            # Task was cancelled upstream — surface as a normal
            # completion with no payload so the agent sees a tool
            # result rather than a hang. The cancel-yielded-tool path
            # is the user-driven side; this branch is for server-side
            # cancels.
            await self._bus.publish(
                event_key,
                {"result": {"isError": False, "content": [
                    {"type": "text",
                     "text": f"task {task_id} was cancelled upstream"},
                ]}},
            )
            return

        if status == "failed":
            await self._bus.publish(
                event_key,
                {"result": {"isError": True, "content": [
                    {"type": "text",
                     "text": (status_result.statusMessage
                              or f"task {task_id} failed")},
                ]}},
            )
            return

        # status == "completed" — fetch the actual payload
        payload_dict = await provider.fetch_task_result(task_id)
        # Strip MCP-internal _meta the resume hook doesn't need; keep
        # everything else (isError, content, structuredContent, ...).
        result_for_bus = {
            k: v for k, v in payload_dict.items() if not k.startswith("_")
        }
        await self._bus.publish(event_key, {"result": result_for_bus})


# ===========================================================================
# Scheduler-flavour park lookup
# ===========================================================================


async def _find_parked_mcp_task_keys(scheduler) -> list[dict]:
    """Return descriptors for sessions parked on ``mcp_task:*`` keys.

    Each descriptor carries the fields the bridge needs:
    ``event_key``, ``toolset_id``, ``task_id``. Resumable rows are
    skipped — they've already been published once; the worker pool
    is responsible for claiming them.
    """
    from primer.scheduler.in_memory import InMemoryScheduler
    from primer.scheduler.postgres import PostgresScheduler

    out: list[dict] = []

    if isinstance(scheduler, InMemoryScheduler):
        async with scheduler._lock:
            for sess in scheduler._sessions.values():
                if (
                    sess.parked_status == "parked"
                    and sess.parked_event_key is not None
                    and sess.parked_event_key.startswith("mcp_task:")
                ):
                    out.append(_extract_mcp_park(sess))
        return out

    if isinstance(scheduler, PostgresScheduler):
        sql = """
            SELECT data->>'parked_event_key' AS event_key,
                   data->'parked_state'      AS parked_state
              FROM sessions
             WHERE data->>'parked_status' = 'parked'
               AND data->>'parked_event_key' LIKE 'mcp_task:%'
             LIMIT 200
        """
        async with scheduler._storage.pool.acquire() as conn:
            rows = await conn.fetch(sql)
        for row in rows:
            # asyncpg returns JSONB as a string unless a codec is
            # registered — parse defensively.
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
                    "toolset_id": meta.get("toolset_id"),
                    "task_id": meta.get("task_id"),
                }
            )
        return out

    return []


def _extract_mcp_park(sess) -> dict:
    blob = sess.parked_state or {}
    yielded = blob.get("yielded") or {}
    meta = yielded.get("resume_metadata") or {}
    return {
        "event_key": sess.parked_event_key,
        "toolset_id": meta.get("toolset_id"),
        "task_id": meta.get("task_id"),
    }


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "McpTaskBridge",
    "TERMINAL_TASK_STATUSES",
]
