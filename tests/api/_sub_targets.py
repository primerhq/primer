"""The workspace and session a subscription points at, as rows (access-control P2 T6).

A subscription's owner must hold full on its target workspace, and a target that does not exist is refused in the words of one the owner holds
nothing on, so a test that saves a subscription seeds the rows it names. An admin clears the level check; a plain user is passed as an owner.
"""

from __future__ import annotations

from datetime import UTC, datetime

from primer.model.access import Access
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession


async def seed_sub_targets(sp, *, owners: list[str] | None = None, workspace_id: str = "ws-1", session_id: str = "sess-1") -> None:
    now = datetime.now(UTC)
    await sp.get_storage(Workspace).create(Workspace(
        id=workspace_id, name=workspace_id, template_id="tpl", provider_id="local-1", created_at=now, phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url=f"ws://{workspace_id}", token="t"), access=Access(owners=owners or []),
    ))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=session_id, workspace_id=workspace_id, binding=AgentSessionBinding(agent_id="ag-1"), status=SessionStatus.WAITING, created_at=now,
    ))
