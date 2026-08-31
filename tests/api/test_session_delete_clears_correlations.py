"""S6 P3: deleting a session drops its thread mappings.

Spec: docs/superpowers/ux-revamp/10-s6-design.md sections 5 and 9. A leaked
mapping would steer a deleted session forever.
"""

from __future__ import annotations

from datetime import UTC, datetime

from primer.channel.correlation import CorrelationStore
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


async def test_delete_session_clears_its_mappings(client, fake_storage_provider):
    await fake_storage_provider.get_storage(WorkspaceSession).create(
        WorkspaceSession(
            id="s-del", workspace_id="w1",
            binding=AgentSessionBinding(agent_id="ag1"),
            status=SessionStatus.ENDED, created_at=datetime.now(UTC),
        )
    )
    store = CorrelationStore(fake_storage_provider)
    await store.upsert_thread_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1",
        session_id="s-del",
    )
    await store.upsert_thread_session(
        channel_id="ch-2", anchor="thr-2", workspace_id="w1",
        session_id="s-keep",
    )

    r = await client.delete("/v1/workspaces/w1/sessions/s-del")
    assert r.status_code == 204, r.text

    assert await store.lookup("ch-1", "thr-1") is None
    assert await store.lookup("ch-2", "thr-2") is not None
