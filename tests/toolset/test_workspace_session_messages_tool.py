"""A-22 follow-up: the supported way to read a session's transcript over MCP.

The ``.state`` guard refuses a non-admin raw read of
``.state/sessions/<sid>/messages.jsonl``, which is how the MCP-as-a-service
cookbook used to read a session's reply. ``read_workspace_session_messages``
is the replacement: the same rule as ``GET /v1/sessions/{sid}/messages``
(``required_role="user"``, the session must exist on that workspace) and the
same reader (``_read_workspace_turn_log``), so a client never needs a raw
state path.
"""

from __future__ import annotations

import json

import pytest

from tests._support.caller import caller
from tests.tap.test_mcp_tap_tool import (
    _WID,
    _FakeWorkspaceIO,
    _Provider,
    _build,
    _line,
    _msg_path,
    _seed_session,
)

TOOL = "read_workspace_session_messages"


async def _setup():
    provider = _Provider()
    io = _FakeWorkspaceIO()
    await _seed_session(provider.store, "s1")
    io.write(
        _msg_path("s1"),
        _line(1, "user_input", text="Reply with exactly: PONG")
        + _line(2, "assistant_token", text="PONG")
        + _line(3, "done"),
    )
    return _build(provider, io), provider, io


async def _call(ts, arguments, ctx):
    return await ts.call(tool_name=TOOL, arguments=arguments, principal=None, ctx=ctx)


def test_the_tool_is_registered_for_users():
    from primer.toolset.workspaces import build_workspaces_toolset

    ts = build_workspaces_toolset(
        storage_provider=_Provider(), workspace_registry=None, tap_router=None,
    )
    assert ts.required_role(TOOL) == "user"


@pytest.mark.parametrize("ctx", [caller("user"), None], ids=["user-run", "mcp"])
async def test_a_user_reads_a_sessions_transcript(ctx):
    ts, _, _ = await _setup()
    res = await _call(ts, {"workspace_id": _WID, "session_id": "s1"}, ctx)
    assert not res.is_error, res.output
    body = json.loads(res.output)
    assert [i["seq"] for i in body["items"]] == [1, 2, 3]
    assert "PONG" in json.dumps(body["items"][1])
    assert body["total"] == 3


async def test_after_seq_and_limit_page_the_transcript():
    ts, _, _ = await _setup()
    res = await _call(
        ts, {"workspace_id": _WID, "session_id": "s1", "after_seq": 1, "limit": 1},
        caller("user"),
    )
    body = json.loads(res.output)
    assert [i["seq"] for i in body["items"]] == [2]


async def test_a_session_on_another_workspace_is_not_found():
    ts, provider, _ = await _setup()
    res = await _call(
        ts, {"workspace_id": "ws-other", "session_id": "s1"}, caller("user"),
    )
    assert res.is_error
    assert json.loads(res.output)["type"] == "not-found"


async def test_an_unknown_session_is_not_found():
    ts, _, _ = await _setup()
    res = await _call(ts, {"workspace_id": _WID, "session_id": "nope"}, caller("user"))
    assert res.is_error
    assert json.loads(res.output)["type"] == "not-found"


async def test_a_session_with_no_log_yet_reads_empty():
    ts, provider, _ = await _setup()
    await _seed_session(provider.store, "s2")
    res = await _call(ts, {"workspace_id": _WID, "session_id": "s2"}, caller("user"))
    assert not res.is_error, res.output
    assert json.loads(res.output)["items"] == []


@pytest.mark.parametrize("ctx", [caller("user"), None], ids=["user-run", "mcp"])
async def test_the_raw_state_read_is_still_refused(ctx):
    """The guard is not weakened: the raw path stays refused."""
    ts, _, _ = await _setup()
    res = await ts.call(
        tool_name="read_workspace_file",
        arguments={"workspace_id": _WID, "path": ".state/sessions/s1/messages.jsonl"},
        principal=None, ctx=ctx,
    )
    assert res.is_error
    assert json.loads(res.output)["type"] == "forbidden"
