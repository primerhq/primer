"""S6 P3: the relay posts into the thread the session is mapped to.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 5 (outbound).
"""

from __future__ import annotations

from datetime import UTC, datetime

from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.channel.session_relay import post_session_final_result
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from tests.conftest import _FakeStorageProvider


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.envelopes: list = []

    async def dispatch_prompt(self, *, envelope, session=None):
        self.envelopes.append(envelope)
        return [{"ok": True}]


def _session(**meta) -> WorkspaceSession:
    return WorkspaceSession(
        id="s1", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.ENDED, created_at=datetime.now(UTC),
        metadata=meta,
    )


async def test_relay_carries_the_mapped_thread_anchor():
    dispatcher = _RecordingDispatcher()
    session = _session(**{
        SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-1", "anchor": "thr-1"},
    })
    sent = await post_session_final_result(
        dispatcher=dispatcher, session=session,
        storage_provider=_FakeStorageProvider(), text="all done",
    )
    assert sent is True
    assert dispatcher.envelopes[0].thread_anchor == "thr-1"


async def test_anchorless_binding_leaves_the_anchor_unset():
    dispatcher = _RecordingDispatcher()
    session = _session(**{
        SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-1"},
    })
    await post_session_final_result(
        dispatcher=dispatcher, session=session,
        storage_provider=_FakeStorageProvider(), text="all done",
    )
    assert dispatcher.envelopes[0].thread_anchor is None
