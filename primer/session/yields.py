"""Service-layer helper for resuming a parked session.

Extracted from the yield-respond REST endpoints so the parked_session
trigger dispatcher (Plan §5.4) can reach the same wake path without
going through HTTP. Spec §5.4.

The yielding-tools surface parks a session on
``sessions.parked_state`` (or ``chats.parked_state``); the row carries
an ``event_key`` inside ``parked_state['yielded']``. Resuming the
yield consists of publishing a payload onto that key — the event bus
listener inside each worker pool catches the publish and atomically
flips ``parked_status`` from ``"parked"`` to ``"resumable"`` so the
next claim loop picks the row up.

The router endpoints in :mod:`primer.api.routers.yields` and
:mod:`primer.api.routers.tool_approval` do this inline today. This
helper consolidates the lookup + validation + publish so:

* The parked_session dispatcher can call it directly from inside the
  trigger fire worker.
* Future yielding-tool resume callers (e.g. MCP bridge, in-process
  unit tests) don't have to re-implement the parked_state walk.

The helper is intentionally tolerant of both Session and Chat parks
— the parked_state shape is identical across both entities — but the
trigger dispatcher only targets workspace sessions today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from primer.model.except_ import NotFoundError
from primer.model.workspace_session import WorkspaceSession


logger = logging.getLogger(__name__)


@dataclass
class RespondToYieldDeps:
    """Collaborators :func:`respond_to_yield` needs.

    Kept tiny on purpose — the helper does one storage lookup and one
    bus publish; everything else lives inside the worker pool's bus
    listener and the resume-classifier in
    :mod:`primer.worker.yield_runtime`.
    """

    storage_provider: Any
    event_bus: Any


def _tool_call_id_for(blob: dict[str, Any]) -> str | None:
    """Resolve tool_call_id from a parked_state blob.

    Mirrors :func:`primer.api.routers.yields._tool_call_id_for`. Worker
    writes it at the top level; older parks may have only had it inside
    ``yielded.resume_metadata``. Falling back keeps the lookup robust
    across upgrades.
    """
    tcid = blob.get("tool_call_id")
    if tcid:
        return tcid
    yielded = blob.get("yielded") or {}
    metadata = yielded.get("resume_metadata") or {}
    return metadata.get("tool_call_id")


async def respond_to_yield(
    *,
    session_id: str,
    tool_call_id: str,
    result: Any,
    deps: RespondToYieldDeps,
) -> None:
    """Publish *result* onto the parked session's resume ``event_key``.

    Steps:

    1. Look up the :class:`WorkspaceSession` row.
    2. Validate the row is parked (or already resumable) and that its
       in-flight ``tool_call_id`` matches.
    3. Pull ``event_key`` out of the parked_state blob.
    4. Publish ``result`` onto that key via the event bus. The bus
       listener inside the worker pool flips the row to ``resumable``.

    Raises
    ------
    NotFoundError
        When the session doesn't exist, isn't parked, or is parked on
        a different ``tool_call_id``.

    Notes
    -----
    The helper does NOT write to ``parked_state`` itself — the worker
    pool's bus listener owns that flip via the scheduler's atomic
    ``mark_resumable``. Calling this helper twice for the same yield is
    a no-op once the first publish has flipped the row to ``resumable``
    (the second publish goes onto the bus too, but ``mark_resumable``
    is idempotent so duplicate flips are harmless).
    """
    storage = deps.storage_provider.get_storage(WorkspaceSession)
    session = await storage.get(session_id)
    if session is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")

    if session.parked_status not in ("parked", "resumable"):
        raise NotFoundError(
            f"Session {session_id!r} has no in-flight yield to resume"
        )
    blob: dict[str, Any] = session.parked_state or {}
    expected = _tool_call_id_for(blob)
    if expected != tool_call_id:
        raise NotFoundError(
            f"No in-flight yield with tool_call_id {tool_call_id!r} "
            f"on session {session_id!r}"
        )

    yielded: dict[str, Any] = blob.get("yielded") or {}
    event_key: str | None = yielded.get("event_key")
    if not event_key:
        # Defensive — every park written by the current runtime carries
        # an event_key. A missing one means a corrupted park; the
        # caller's only sensible recourse is to surface 404 like the
        # REST endpoint does.
        raise NotFoundError(
            f"Session {session_id!r} park is missing event_key"
        )

    payload: dict[str, Any]
    if isinstance(result, dict):
        payload = result
    else:
        payload = {"response": result}
    await deps.event_bus.publish(event_key, payload)


__all__ = ["RespondToYieldDeps", "respond_to_yield"]
