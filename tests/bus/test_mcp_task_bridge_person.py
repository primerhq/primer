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


async def _store_person_and_toolset(sp, *, disabled: bool = False, granted: bool = True) -> None:
    from datetime import UTC, datetime

    from primer.model.access import Access
    from primer.model.provider import Toolset
    from primer.model.user import User

    await sp.get_storage(User).create(User(id="u-turn", username="turn", role="user", disabled=disabled, created_at=datetime.now(UTC)))
    await sp.get_storage(Toolset).create(Toolset(
        id="srv-1", provider="mcp", config={"transport": "stdio", "config": {"command": ["echo"]}},
        access=Access(owners=["u-owner"], grants={"u-turn": "read"} if granted else {}),
    ))


async def _poll_with_storage(sp) -> tuple[list[Person], list[tuple[str, dict]]]:
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        sess = _make_mcp_task_parked_session(session_id="sess-1", tool_call_id="tc-1", toolset_id="srv-1", task_id="t-abc")
        sess.turn_person = _ref("u-turn")
        scheduler._sessions["sess-1"] = sess
        provider = _RecordingProvider(_FakeSession(tools=[], task_id="t-abc"), toolset_id="srv-1")
        bridge = McpTaskBridge(bus=bus, scheduler=scheduler, provider_registry=_MockProviderRegistry({"srv-1": provider}), storage_provider=sp)
        woken: list[tuple[str, dict]] = []

        async def record(event_key: str, payload: dict) -> None:
            woken.append((event_key, payload))

        bridge._publish_wake = record  # type: ignore[method-assign]
        [park] = await _find_parked_mcp_task_keys(scheduler)
        await bridge._handle_one_park(park)
        return provider.people, woken
    finally:
        await scheduler.aclose()
        await bus.aclose()


@pytest.mark.parametrize("lost", ["disabled", "access removed"])
async def test_a_person_who_lost_the_toolset_ends_the_parked_call(fake_storage_provider, lost):
    """Review finding 4: before each poll the bridge re-reads the person. One disabled, or no longer able to use the toolset, is not polled for
    with their token: the parked call ends as failed with the reason, so the bridge stops following it."""
    await _store_person_and_toolset(fake_storage_provider, disabled=lost == "disabled", granted=lost != "access removed")

    people, woken = await _poll_with_storage(fake_storage_provider)

    assert people == [], "the bridge polled with the token of a person who may no longer use the toolset"
    [(event_key, payload)] = woken
    assert event_key == "mcp_task:srv-1:t-abc" and payload["result"]["isError"] is True
    assert "may no longer use the MCP toolset srv-1" in payload["result"]["content"][0]["text"]


async def test_a_person_who_still_may_use_the_toolset_is_polled_for(fake_storage_provider):
    await _store_person_and_toolset(fake_storage_provider)

    people, woken = await _poll_with_storage(fake_storage_provider)

    assert people == [Person("u-turn", "user")] * 2 and woken[0][1]["result"]["isError"] is False
