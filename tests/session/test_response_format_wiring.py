"""AG-M3: a session turn is run with its structured-output schema.

Precedence (``primer.session.response_format``): the one-turn override a steer stamps beats the session's persistent
``response_format``; with neither the executor falls back to its agent's own ``response_format`` (ruling D14, pinned in
``tests/agent/test_agent_response_format_default.py``). The override is consumed by the turn that runs with it: the running
flip clears it, so a retry of that turn falls back. Neither reached the executor before, and the override stayed on the row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import AgentSessionBinding, GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.response_format import EPHEMERAL_KEY
from tests.conftest import _FakeStorageProvider
from tests.session.test_dispatch import FakeWorkspaceIO, _make_lease

_SESSION = {"type": "object", "title": "session"}
_ONCE = {"type": "object", "title": "ephemeral"}


class _Recording:
    last_done_reason = "stop"

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def invoke(self, messages, **kwargs):
        self.seen.append(kwargs)
        yield TextDelta(text="{}", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


async def _turns(n: int, **row_fields):
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(**{
        "id": "s1", "workspace_id": "w1", "binding": AgentSessionBinding(agent_id="a"), "status": SessionStatus.RUNNING,
        "created_at": datetime.now(timezone.utc), "turn_status": "claimable", **row_fields,
    }))
    bus = InMemoryEventBus()
    await bus.initialize()
    ex = _Recording()

    async def build(_s):
        return ex

    deps = SessionDispatchDeps(storage_provider=sp, workspace_io=FakeWorkspaceIO(), event_bus=bus, build_executor=build)
    for turn_no in range(1, n + 1):
        await run_one_session_turn(_make_lease("s1"), deps)
        # What the claim's release does: the next turn is a new one (else the completed-turn guard no-ops it).
        await sessions.patch_if("s1", {"turn_no": turn_no}, where={"workspace_id": ["w1"]})
    return ex.seen, await sessions.get("s1")


@pytest.mark.asyncio
async def test_the_session_format_reaches_every_turn():
    seen, _ = await _turns(2, response_format=_SESSION)
    assert seen == [{"response_format": _SESSION}, {"response_format": _SESSION}]


@pytest.mark.asyncio
async def test_the_one_turn_override_is_used_once_then_cleared_from_the_row():
    seen, row = await _turns(2, response_format=_SESSION, metadata={EPHEMERAL_KEY: _ONCE, "keep": 1})
    assert seen == [{"response_format": _ONCE}, {"response_format": _SESSION}]
    assert (row.metadata or {}).get(EPHEMERAL_KEY) is None
    assert row.metadata["keep"] == 1


@pytest.mark.asyncio
async def test_the_override_alone_is_used_once():
    seen, _ = await _turns(2, metadata={EPHEMERAL_KEY: _ONCE})
    assert seen == [{"response_format": _ONCE}, {}]


@pytest.mark.asyncio
async def test_a_session_with_no_format_sends_none_and_its_metadata_is_left_alone():
    seen, row = await _turns(1)
    assert seen == [{}]
    assert row.metadata == {}


@pytest.mark.asyncio
async def test_a_graph_session_is_never_handed_a_format():
    """A graph executor's ``invoke`` takes no ``response_format`` (each agent node carries its own)."""
    seen, row = await _turns(1, binding=GraphSessionBinding(graph_id="g"), response_format=_SESSION,
                             metadata={EPHEMERAL_KEY: _ONCE})
    assert seen == [{}]
