"""Steer and restart leave the session's launch-time base_prompt byte-identical (Wiki First W4).

``base_prompt`` is written exactly once, by the create: there is no update
or patch route for a session row, and a steer (``wake_session``) or a
restart (``reset_session`` + wake) may rewrite the row's status, turn and
invocation bookkeeping - never this field. The static side of the pin
(which modules may even name the field) lives in
``tests/session/test_base_prompt_write_once.py``; this file is the
behavioural side: a steer and a restart on a row that carries a base
prompt leave it byte-identical.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from tests.session._peer_fakes import FakeWorkspace

BASE = "ALWAYS: answer the token question with TOKEN-42"
WID, AGENT = "ws-bp", "ag-1"


def _now() -> datetime:
    return datetime(2026, 11, 9, 10, 0, 0, tzinfo=timezone.utc)


async def _seed(sp, *, sid: str, status: SessionStatus, **fields) -> WorkspaceSession:
    row = WorkspaceSession(
        id=sid,
        workspace_id=WID,
        binding=AgentSessionBinding(agent_id=AGENT),
        status=status,
        created_at=_now(),
        base_prompt=BASE,
        **fields,
    )
    await sp.get_storage(WorkspaceSession).create(row)
    return row


@pytest.fixture
async def fake_workspace(app):
    """The app's registry answers every workspace with one fake workspace."""
    ws = FakeWorkspace()

    async def _get(workspace_id: str):
        return ws

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    return ws


async def test_a_steer_keeps_the_base_prompt_byte_identical(
    client, fake_storage_provider, fake_workspace,
):
    await _seed(
        fake_storage_provider, sid="s-steer", status=SessionStatus.WAITING, turn_no=1,
    )

    resp = await client.post(
        f"/v1/workspaces/{WID}/sessions/s-steer/steer",
        json={"instruction": "keep going"},
    )
    assert resp.status_code == 200, resp.text

    row = await fake_storage_provider.get_storage(WorkspaceSession).get("s-steer")
    assert row.base_prompt == BASE, "a steer wrote the base prompt"


async def test_a_restart_keeps_the_base_prompt_byte_identical(
    client, fake_storage_provider, fake_workspace,
):
    await _seed(
        fake_storage_provider,
        sid="s-restart",
        status=SessionStatus.ENDED,
        ended_reason="completed",
        ended_at=_now(),
        turn_no=1,
    )

    resp = await client.post(
        f"/v1/workspaces/{WID}/sessions/s-restart/restart",
        json={"input": "go again"},
    )
    assert resp.status_code == 200, resp.text

    row = await fake_storage_provider.get_storage(WorkspaceSession).get("s-restart")
    assert row.base_prompt == BASE, "a restart wrote the base prompt"
    # The contrast: the restart's input is a new USER_INPUT record in the
    # log; it does not even rewrite the row's own initial_instructions.
    assert row.initial_instructions is None
