"""``has_open_turn`` reads its window in ORDER (ticket 01a11cc7): a terminal that comes before the first open input closes nothing.

It used to compare two totals, inputs against terminals, so a terminal at the front of the window (the claim adapter's release marker is written at
the very seq the failure exit moved the drain cursor to) cancelled out an input that arrived after it and the window read as closed while that input
was unanswered. The last test builds the window with the production writers instead of a hand-made log: a turn that fails, the release of that
failed turn, then a message to the failed session (which reopens it).
"""

from __future__ import annotations

import json

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.int.claim import ReleaseOutcome
from primer.model.chat import TextDelta
from primer.model.workspace_session import WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.turns import count_turn_state, has_open_turn
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeExecutor,
    FakeWorkspaceIO,
    _make_lease,
    _seed_session,
    fake_event_bus,
    fake_storage_provider,
)


def _rec(seq, kind, **payload):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload, "created_at": "2026-10-08T00:00:00+00:00"})


def test_a_terminal_before_the_first_input_closes_nothing():
    lines = [_rec(1, "error", reason="unknown", terminal=True), _rec(2, "user_input", text="again")]

    assert has_open_turn(lines, cursor=0) is True


def test_a_terminal_closes_the_input_before_it_and_not_the_one_after_it():
    closed = [_rec(1, "user_input", text="a"), _rec(2, "done"), _rec(3, "error", message="m", code="c")]
    reopened = [*closed, _rec(4, "user_input", text="b")]

    assert has_open_turn(closed, cursor=0) is False
    assert has_open_turn(reopened, cursor=0) is True


def test_queued_inputs_still_need_a_terminal_each():
    lines = [_rec(1, "user_input", text="a"), _rec(2, "user_input", text="b"), _rec(3, "done")]

    assert has_open_turn(lines, cursor=0) is True
    assert has_open_turn([*lines, _rec(4, "done")], cursor=0) is False


def test_the_totals_are_still_reported_as_totals():
    tc = count_turn_state([_rec(1, "error", terminal=True), _rec(2, "user_input", text="a")], cursor=0)

    assert (tc.open_user_inputs, tc.terminals, tc.max_seen_seq) == (1, 1, 2)


# ---- the production writers -------------------------------------------------------------------------------------------------------------------


class _Slot:
    async def reopen(self) -> None: ...

    async def append_instruction(self, content, *, extra_parts=None) -> None: ...


class _Workspace(FakeWorkspaceIO):
    async def get_session(self, session_id):
        return _Slot()


class _Registry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id):
        return self._workspace

    async def get_workspace_row(self, workspace_id):
        return None


class _Scheduler:
    async def enqueue(self, session_id) -> None: ...


class _Engine:
    async def upsert(self, *args, **kwargs) -> None: ...


@pytest.mark.asyncio
async def test_a_failed_session_that_is_written_to_again_has_an_open_turn(fake_storage_provider, fake_event_bus):
    sid = "s-order"
    workspace = _Workspace()
    await _seed_session(fake_storage_provider, sid)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(sid)
    await sessions.update(row.model_copy(update={"turn_no": 3, "completed_turn_no": 2}))

    async def build(_session):
        return FakeExecutor([TextDelta(text="hi", index=0), RuntimeError("the executor blew up")])

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=workspace, event_bus=fake_event_bus, build_executor=build,
    )
    failed = await run_one_session_turn(_make_lease(sid), deps)
    assert failed.success is False
    # the pool's release of that failed turn: the claim adapter writes its terminal marker into the log
    adapter = SessionClaimAdapter(
        session_storage=sessions, workspace_registry=_Registry(workspace), event_bus=fake_event_bus,
    )
    await adapter.on_release(None, sid, outcome=failed)
    # a message to the failed session reopens it and lands as a USER_INPUT
    reopened = await wake_session(
        workspace_id=row.workspace_id, session_id=sid, instruction="try again", human_intent=True,
        deps=SessionWakeDeps(
            storage_provider=fake_storage_provider, scheduler=_Scheduler(), claim_engine=_Engine(),
            workspace_registry=_Registry(workspace), event_bus=fake_event_bus,
        ),
    )

    lines = workspace.read_lines(sid)
    records = [json.loads(line) for line in lines if '"seq"' in line]
    # The premise, pinned on the window the guard reads (kinds only: the guard judges the order of the records at and after the cursor, not their seq values): the failed turn's terminal sits AT the cursor,
    # ahead of the input that reopened the session. Checking the whole log would pass on an ERROR written below the cursor.
    window = [record["kind"] for record in records if record["seq"] >= reopened.next_unprocessed_seq]
    assert window == ["error", "invocation_divider", "user_input"], window
    assert has_open_turn(lines, cursor=reopened.next_unprocessed_seq) is True, (
        "the message that reopened the failed session is unanswered, but the failed turn's terminals at the cursor were counted against it"
    )
