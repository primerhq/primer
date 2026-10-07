"""A user Cancel that preempts a turn DURING the stream lands the one cancelled exit (console review 2026-10-08, C-011).

The Cancel route flags the row and signals the pool, whose ``_cancel_loop`` / row reconciler HARD-cancel the turn task
(``cancel_once``). A turn blocked in a long model call never reaches the cooperative ``cancel_event`` check (it only looks
between events), so for a slow turn the hard cancel IS how a Cancel arrives. The ``CancelledError`` used to leave
``run_one_session_turn`` without entering ``_land_cancelled_turn``: the pool's preempt convergence ended the row behind the
tap's back, with no CANCELLED record, no tick (so no terminal frame ever reached a client, which then showed "running" for
good), no terminal event (the interactive webhook hold waits on it), no turn-log entry, no metric, and whatever the dead turn
had buffered or streamed was lost.

The cancel is now told apart on the ROW, as the pool's convergence does: ``cancel_requested`` set and the row not ended is a user
Cancel and takes the same exit as every other cancelled turn; anything else (a lease stolen or expired, the drain timeout, a
force-delete that already ended the row) is not this turn's to land and propagates exactly as before.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from primer.model.chat import TextDelta
from primer.model.workspace_session import SessionMessageKind, SessionStatus, WorkspaceSession
from primer.session.dispatch import run_one_session_turn
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeWorkspaceIO,
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)
from tests.session.test_dispatch_interrupt import _deps


class _BlocksInTheModelCall:
    """Streams some text, then waits for a model that does not answer. It does not take the Stop event, so only the pool's
    hard cancel can end the turn (dispatch looks at its own event between stream events only)."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()

    async def invoke(self, messages: list[Any], **kwargs: Any):
        yield TextDelta(text="working on it", index=0)
        self.reached.set()
        await asyncio.Event().wait()


def _records(io: FakeWorkspaceIO, session_id: str) -> list[dict]:
    return [json.loads(line) for line in io.read_lines(session_id)]


async def _flag_cancel(storage, sid: str, **changes) -> None:
    """What ``cancel_session`` does to a RUNNING session (the flag and its time), plus any other change a case needs."""
    sessions = storage.get_storage(WorkspaceSession)
    row = await sessions.get(sid)
    row.cancel_requested = True
    for name, value in changes.items():
        setattr(row, name, value)
    await sessions.update(row)


async def _start(sid, storage, io, bus, executor) -> asyncio.Task:
    task = asyncio.ensure_future(run_one_session_turn(_make_lease(sid), _deps(storage, io, bus, executor)))
    await asyncio.wait_for(executor.reached.wait(), 3.0)
    return task


class TestAUserCancelThatPreemptsTheStream:
    async def test_it_takes_the_one_cancelled_exit(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        sid = seeded_session.id
        published: list[tuple[str, dict]] = []
        publish = fake_event_bus.publish

        async def spy_publish(key: str, payload: dict) -> None:
            published.append((key, payload))
            await publish(key, payload)

        monkeypatch.setattr(fake_event_bus, "publish", spy_publish)
        executor = _BlocksInTheModelCall()
        task = await _start(sid, fake_storage_provider, fake_workspace_io, fake_event_bus, executor)

        await _flag_cancel(fake_storage_provider, sid)
        task.cancel()                                       # the pool's cancel_once("user_signal")
        outcome = await asyncio.wait_for(task, 5.0)         # used to raise CancelledError

        assert outcome.success and outcome.drop_lease, "the exit's own outcome is what the pool releases with"
        assert task.cancelling() == 0, "the absorbed cancellation was left pending on the task"
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled"
        records = _records(fake_workspace_io, sid)
        kinds = [r["kind"] for r in records]
        assert kinds[-1] == SessionMessageKind.CANCELLED, f"the transcript must end in CANCELLED, got {kinds}"
        assert records[-1]["payload"]["reason"] == "operator_cancel"
        streamed = [r for r in records if r["kind"] == SessionMessageKind.ASSISTANT_TOKEN]
        assert streamed and "working on it" in json.dumps(streamed[0]["payload"]), (
            "the text the model had already streamed is made durable ahead of the CANCELLED record"
        )
        assert (f"session:{sid}:tick", {"seq": records[-1]["seq"]}) in published, "no tick told the tap about the CANCELLED record"
        assert (f"session:{sid}:terminal", {"status": "ended", "ended_reason": "cancelled"}) in published, (
            "the terminal event the interactive webhook hold waits on was lost"
        )

    async def test_a_cancel_that_is_not_flagged_on_the_row_is_not_this_turns_to_land(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """A lease stolen or expired and the drain timeout cancel the task too. Another worker owns a stolen session, so this
        one must leave the row and the log alone and let the cancellation through to the pool."""
        sid = seeded_session.id
        executor = _BlocksInTheModelCall()
        task = await _start(sid, fake_storage_provider, fake_workspace_io, fake_event_bus, executor)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5.0)

        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status != SessionStatus.ENDED and not row.cancel_requested
        assert SessionMessageKind.CANCELLED not in [r["kind"] for r in _records(fake_workspace_io, sid)]

    async def test_a_force_deleted_row_is_left_to_the_delete(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """A force-delete flags the row and ends it ENDED/force_deleted, then removes it and its slot: writing a CANCELLED
        record would recreate a transcript in a deleted session's workspace, and the reason must stay."""
        sid = seeded_session.id
        executor = _BlocksInTheModelCall()
        task = await _start(sid, fake_storage_provider, fake_workspace_io, fake_event_bus, executor)

        await _flag_cancel(fake_storage_provider, sid, status=SessionStatus.ENDED, ended_reason="force_deleted")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5.0)

        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.ENDED and row.ended_reason == "force_deleted"
        assert SessionMessageKind.CANCELLED not in [r["kind"] for r in _records(fake_workspace_io, sid)]

    async def test_a_row_that_cannot_be_read_does_not_replace_the_cancellation_with_its_error(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """The pool's preempt convergence runs only on a ``CancelledError``: a storage error raised in its place would skip it
        and leave the session RUNNING with no lease."""
        sid = seeded_session.id
        sessions = fake_storage_provider.get_storage(WorkspaceSession)
        real_get = sessions.get
        turn: dict[str, asyncio.Task] = {}
        spent = {"failed": False}

        async def get_that_fails_once_in_the_cancelled_task(session_id: str):
            # Only the read made by the turn task itself while it is being cancelled (the decision), and only once: the cancel
            # watcher's own poll and the cleanup's reads must not use the failure up.
            me = asyncio.current_task()
            if me is turn.get("task") and me.cancelling() and not spent["failed"]:
                spent["failed"] = True
                raise RuntimeError("storage is down")
            return await real_get(session_id)

        sessions.get = get_that_fails_once_in_the_cancelled_task
        try:
            executor = _BlocksInTheModelCall()
            turn["task"] = await _start(sid, fake_storage_provider, fake_workspace_io, fake_event_bus, executor)
            await _flag_cancel(fake_storage_provider, sid)
            turn["task"].cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(turn["task"], 5.0)
            assert spent["failed"], "the decision's row read never happened in the cancelled task"
        finally:
            sessions.get = real_get
