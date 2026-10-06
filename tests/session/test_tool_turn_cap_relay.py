"""A turn stopped by the agent's ``max_tool_turns`` is counted and relayed as STOPPED SHORT, not as a normal completion.

Ticket 01a1095e, PR-A (the lead's ruling 2026-10-07). Before: the completion exit counted every capped turn ``completed``
and the channel relay's three arms (``relay_every_turn``, ENDED/``completed``, a clean stop) matched a cap trip only by
accident: a thread-mapped session posted its partial text as if it were the answer (or nothing, when it had none), an
AUTONOMOUS capped run (ENDED/``tool_turn_cap``) posted NOTHING AT ALL, and a plain interactive one (WAITING) posted
nothing. Now each of the three shapes posts one message that says the run stopped at its tool-turn cap (with what the
agent had so far, when it had something), and the turn is counted under its own ``status`` label value.

The harness is ``test_per_turn_relay.py``'s: the real ``run_one_session_turn``, a scripted executor that publishes
``last_done_reason`` as the real one does after a cap trip, a recording channel dispatcher.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import primer.observability.metrics as metrics
from primer.bus.in_memory import InMemoryEventBus
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.model.chat import Done, TextDelta
from primer.model.envelope import RELAY_EVERY_TURN_KEY
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn

from tests.conftest import _FakeStorageProvider
from tests.session.test_per_turn_relay import (
    _BINDING,
    _FakeWorkspaceIO,
    _lease,
    _RecordingDispatcher,
    _ScriptedExecutor,
)

PARTIAL = "let me check one more thing"


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


async def _capped_turn(*, metadata: dict, autonomous: bool | None = None, text: str | None = PARTIAL):
    """One turn the cap stopped: the model's last event is ``Done(tool_use)``, the executor publishes ``tool_turn_cap``."""
    sp = _FakeStorageProvider()
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC), turn_status="running", autonomous=autonomous, metadata=metadata,
    ))
    bus = InMemoryEventBus()
    await bus.initialize()
    dispatcher = _RecordingDispatcher()
    events = ([TextDelta(text=text, index=0)] if text else []) + [Done(stop_reason="tool_use", raw_reason="tool_use")]

    async def _build(session):
        return _ScriptedExecutor(events, last_done_reason="tool_turn_cap")

    await run_one_session_turn(_lease("s1"), SessionDispatchDeps(
        storage_provider=sp, workspace_io=_FakeWorkspaceIO(), event_bus=bus, build_executor=_build,
        channel_dispatcher=dispatcher,
    ))
    await bus.aclose()
    row = await sp.get_storage(WorkspaceSession).get("s1")
    return dispatcher, row


def _says_stopped_short(message: str) -> bool:
    return "tool-turn cap" in message and message != PARTIAL


@pytest.mark.asyncio
async def test_an_autonomous_capped_run_posts_a_stopped_short_message():
    """The silent shape: ENDED/tool_turn_cap matched no relay arm, so the channel never heard the run stopped short."""
    dispatcher, row = await _capped_turn(metadata={SESSION_REPLY_BINDING_KEY: _BINDING}, autonomous=True)

    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "tool_turn_cap"), "the harness is not the autonomous cap shape"
    (message,) = dispatcher.texts
    assert _says_stopped_short(message), message
    assert PARTIAL in message, "what the agent had so far was dropped"


@pytest.mark.asyncio
async def test_a_thread_mapped_capped_turn_is_flagged_not_posted_as_the_answer():
    dispatcher, row = await _capped_turn(metadata={SESSION_REPLY_BINDING_KEY: _BINDING, RELAY_EVERY_TURN_KEY: True})

    assert (row.status, row.ended_reason) == (SessionStatus.WAITING, None)
    (message,) = dispatcher.texts
    assert _says_stopped_short(message), f"the partial text was posted as if it were the answer: {message!r}"


@pytest.mark.asyncio
async def test_a_plain_interactive_capped_turn_with_a_reply_binding_posts_a_stopped_short_message():
    dispatcher, _row = await _capped_turn(metadata={SESSION_REPLY_BINDING_KEY: _BINDING})

    (message,) = dispatcher.texts
    assert _says_stopped_short(message), message


@pytest.mark.asyncio
async def test_a_capped_turn_that_streamed_nothing_still_says_it_stopped_short():
    dispatcher, _row = await _capped_turn(metadata={SESSION_REPLY_BINDING_KEY: _BINDING}, autonomous=True, text=None)

    (message,) = dispatcher.texts
    assert "tool-turn cap" in message


@pytest.mark.asyncio
async def test_a_session_with_no_reply_binding_posts_nothing():
    dispatcher, _row = await _capped_turn(metadata={}, autonomous=True)

    assert dispatcher.texts == []


@pytest.mark.asyncio
async def test_a_quiet_binding_suppresses_the_stopped_short_message():
    dispatcher, _row = await _capped_turn(metadata={SESSION_REPLY_BINDING_KEY: {**_BINDING, "quiet": True}}, autonomous=True)

    assert dispatcher.texts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("autonomous", [True, None], ids=["autonomous", "interactive"])
async def test_a_capped_turn_is_counted_under_its_own_status(autonomous):
    await _capped_turn(metadata={}, autonomous=autonomous)

    assert metrics.turns_total.labels("ag1", "tool_turn_cap")._value.get() == 1.0
    assert metrics.turns_total.labels("ag1", "completed")._value.get() == 0.0, "a capped turn was counted completed"
    assert metrics.turn_duration_seconds.labels("ag1", "tool_turn_cap")._sum.get() >= 0.0
