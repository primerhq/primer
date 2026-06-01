"""WorkspaceSession factory.

Extracted from ``POST /v1/workspaces/{wid}/sessions``
(:mod:`primer.api.routers.sessions`) so the REST endpoint and the
trigger dispatcher (Phase 4+) share a single canonical session-create
code path. Spec §12.5 (Plan §3.2).

Scope
-----

The factory owns the *persistence* + *auto-start* + *claim/scheduler
registration* steps. It deliberately does NOT do:

* binding validation (agent / graph existence, ``graph_input`` schema
  check) — the REST router has structured 404/422 mapping it must keep.
* on-disk session slot allocation via
  :meth:`Workspace.start_session` — only the router needs that today
  because the trigger dispatcher targets an existing parent session
  (subscriber kind ``existing_session``) or creates a fresh chat row
  rather than a workspace session. When subscription kinds for
  fresh-workspace-sessions land we add slot allocation behind the
  optional ``workspace_registry`` dep.

Callers that need the full router behaviour pre-validate the binding,
then invoke :func:`create_session` with the validated inputs. The
helper handles the rest atomically.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from primer.int.claim import ClaimKind
from primer.model.workspace_session import (
    GraphSessionBinding,
    SessionBinding,
    SessionStatus,
    WorkspaceSession,
)


logger = logging.getLogger(__name__)


@dataclass
class SessionFactoryDeps:
    """Bundle of collaborators :func:`create_session` needs.

    ``workspace_registry`` is optional: when present (today: REST
    router), the factory will allocate the on-disk session slot via the
    backend. When ``None`` (today: trigger dispatcher's existing-session
    target case) the factory just writes the scheduler-visible row.
    """

    storage_provider: Any
    claim_engine: Any
    scheduler: Any
    workspace_registry: Any | None = None


async def create_session(
    *,
    workspace_id: str,
    binding: SessionBinding,
    initial_instructions: str | None,
    graph_input: Any | None,
    auto_start: bool,
    metadata: dict | None,
    deps: SessionFactoryDeps,
    parent_session_id: str | None = None,
    session_id: str | None = None,
) -> WorkspaceSession:
    """Persist a :class:`WorkspaceSession` row + optionally auto-start.

    Steps mirror :func:`primer.api.routers.sessions.create_session` so
    the two call paths produce identical rows:

    1. Persist the row with ``status=CREATED``.
    2. Fold ``graph_input`` into ``metadata['graph_input']`` for graph
       bindings.
    3. If ``auto_start``: flip to ``RUNNING``, stamp ``started_at``,
       and call ``scheduler.enqueue(sid)`` (best-effort — a broken
       scheduler must not strand the row).
    4. Always upsert with the :class:`ClaimEngine` so the worker pool
       sees the row (forward-compat; no-op when not wired).

    Returns the persisted (and possibly auto-started) session row.

    ``session_id`` lets the caller pre-generate the id so it can run
    its own setup (e.g., on-disk slot allocation) before the row lands
    in storage. When ``None``, a fresh ``sess-<hex>`` id is generated.
    """
    sid = session_id if session_id is not None else f"sess-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)

    md: dict[str, Any] = dict(metadata or {})
    # Graph bindings carry graph_input on metadata so the workspace
    # graph executor can pick it up as the initial input. Mirrors the
    # router's behaviour at primer/api/routers/sessions.py.
    if isinstance(binding, GraphSessionBinding) and graph_input is not None:
        md["graph_input"] = graph_input

    session = WorkspaceSession(
        id=sid,
        workspace_id=workspace_id,
        binding=binding,
        status=SessionStatus.CREATED,
        parent_session_id=parent_session_id,
        initial_instructions=initial_instructions,
        metadata=md,
        created_at=now,
    )
    sessions_storage = deps.storage_provider.get_storage(WorkspaceSession)
    await sessions_storage.create(session)

    if auto_start:
        session.status = SessionStatus.RUNNING
        session.started_at = now
        await sessions_storage.update(session)
        # Best-effort scheduler enqueue: the row is the source of truth.
        # A scheduler outage must not roll back the session.
        try:
            await deps.scheduler.enqueue(sid)
        except Exception as exc:  # noqa: BLE001 — defensive perimeter
            logger.warning(
                "session_factory: scheduler.enqueue(%r) raised: %s",
                sid, exc,
            )

    # Forward-compat ClaimEngine upsert — matches what the REST router
    # has always done. No-op when ``claim_engine is None``.
    if deps.claim_engine is not None:
        try:
            await deps.claim_engine.upsert(ClaimKind.SESSION, sid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session_factory: claim_engine.upsert(%r) raised: %s",
                sid, exc,
            )

    return session


__all__ = ["SessionFactoryDeps", "create_session"]
