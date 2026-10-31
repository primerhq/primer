"""The MCP task bridge polls a parked task call as the parked run's person (access-control plan P5 Task 1).

The bridge runs in the background with nobody acting, so without this a browser-OAuth toolset's ``tasks/get`` and ``tasks/result`` would get no
token. It reads the person from the parked session (its ``turn_person``, else its ``initiated_by``) and makes both upstream calls under
``acting_as`` that person; the provider picks the token with ``person_for_tool(None)``, which reads the acting person. A park that names nobody
gets nobody (no browser token).
"""

from __future__ import annotations

import pytest

from primer.access.acting import acting_person
from primer.access.person import Person, person_for_tool
from primer.bus.in_memory import InMemoryEventBus
from primer.bus.mcp_tasks import McpTaskBridge, _find_parked_mcp_task_keys
from primer.model.principal import PrincipalRef
from primer.scheduler.in_memory import InMemoryScheduler
from tests.toolset.test_mcp_tasks import _FakeSession, _FakeStdioProvider, _make_mcp_task_parked_session, _MockProviderRegistry


def _ref(user_id: str) -> PrincipalRef:
    return PrincipalRef(type="user", id=user_id, display=user_id, role="user", source="local", user_id=user_id)


class _RecordingProvider(_FakeStdioProvider):
    """Records, for every upstream session it opens, the person the real provider would pick the OAuth token for."""

    def __init__(self, session, *, toolset_id: str) -> None:
        super().__init__(session, toolset_id=toolset_id)
        self.people: list[Person] = []

    def _open_session(self, *, principal=None, ctx=None):  # type: ignore[override]
        self.people.append(person_for_tool(ctx))
        return super()._open_session(principal=principal, ctx=ctx)


async def _poll_once(turn_person: PrincipalRef | None, initiated_by: PrincipalRef | None) -> list[Person]:
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        sess = _make_mcp_task_parked_session(session_id="sess-1", tool_call_id="tc-1", toolset_id="srv-1", task_id="t-abc")
        sess.turn_person, sess.initiated_by = turn_person, initiated_by
        scheduler._sessions["sess-1"] = sess
        provider = _RecordingProvider(_FakeSession(tools=[], task_id="t-abc"), toolset_id="srv-1")
        bridge = McpTaskBridge(bus=bus, scheduler=scheduler, provider_registry=_MockProviderRegistry({"srv-1": provider}))
        [park] = await _find_parked_mcp_task_keys(scheduler)

        await bridge._handle_one_park(park)

        assert acting_person() is None, "the bridge's person leaked out of the poll"
        return provider.people
    finally:
        await scheduler.aclose()
        await bus.aclose()


@pytest.mark.parametrize(
    ("turn_person", "initiated_by", "expected"),
    [
        (_ref("u-turn"), _ref("u-start"), Person("u-turn", "user")),
        (None, _ref("u-start"), Person("u-start", "user")),
        (None, None, Person(None, "user")),
    ],
    ids=["the turn's person", "the initiator", "nobody"],
)
async def test_the_bridge_polls_as_the_parked_runs_person(turn_person, initiated_by, expected):
    people = await _poll_once(turn_person, initiated_by)

    assert people == [expected, expected], "tasks/get and tasks/result both act for the parked run's person"
