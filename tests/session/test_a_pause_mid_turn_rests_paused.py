"""A pause that lands while a turn runs rests the session PAUSED at the end of that turn (ticket 01a16d9f (b), the lead's ruling on #904's review).

The pause (``POST .../pause`` and ``workspaces__pause_workspace_session``, one helper) writes ``pause_requested=True`` on a RUNNING row; the turn in
flight does not see it and finishes. Its end wrote WAITING regardless, so the session read ``waiting`` with the flag set, which the console showed as
"Ready", and a person's message to it queued behind any message the pause held instead of resuming it (a PAUSED session is woken by a person's
message, a WAITING one with queued messages is not). Only a later claim would have written PAUSED, and nothing claims a row the pause hold keeps.

Now a turn that comes to rest (its clean end, a Stop) writes PAUSED when a pause is on the row, with the flag kept as the start-of-turn pause exit
keeps it, and so does a failed turn that rests; a turn that ENDS the session still ends it (an autonomous session's clean end, a failure that ends):
a pause acts between turns and never changes a turn's own outcome, and an autonomous session has no next turn to hold (its graph or one-shot run
is done; resuming it would run it again).

Every test runs on real SQLite with the REAL dispatcher (``run_one_session_turn``) and the REAL agent executor over a real local workspace, in one
process and across two (the shape of ``tests/session/test_pause_and_resume_tools_take_the_rest_path.py``, whose worlds these reuse). Bounded.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from primer.model.chat import Done, TextDelta
from primer.model.except_ import RateLimitError
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.dispatch import run_one_session_turn
from primer.session.steer_delivery import DELIVERED_QUEUED, DELIVERED_WOKEN, deliver_steer
from tests.agent.test_workspace_executor import _FakeLLM
from tests.session.test_dispatch import _make_lease, fake_event_bus  # noqa: F401  (a fixture, used by name)
from tests.session.test_pause_and_resume_tools_take_the_rest_path import OK, _close, _Process, _Registry, _rest, _tool, _world

pytestmark = pytest.mark.asyncio

BOUND_S = 60
PROCESSES = pytest.mark.parametrize("two_processes", [False, True], ids=["one process", "another process"])


class _ActingLLM(_FakeLLM):
    """Runs ``action`` inside the model call, while the turn is in flight (the dispatcher holds no lifecycle lock there), then streams its script.
    ``then`` replaces the script: ``"raise"`` fails the call with a rate limit (a failure an interactive session rests after), ``"hang"`` never
    finishes it (only a Stop ends the turn)."""

    def __init__(self, action, *, then: str = "stream", **kwargs) -> None:
        super().__init__(**kwargs)
        self._action = action
        self._then = then

    def stream(self, *, model, messages, **kwargs):
        inner = super().stream(model=model, messages=messages, **kwargs)
        action, then = self._action, self._then

        async def events():
            yield TextDelta(text="working on it", index=0)
            await action()
            if then == "raise":
                raise RateLimitError("the provider is rate limiting this key")
            if then == "hang":
                await asyncio.Event().wait()
            async for ev in inner:
                yield ev

        return events()


def _pauser(api: _Process, row: WorkspaceSession, surface: str, bus, answers: list) -> Any:
    async def pause() -> None:
        answers.append(await (_tool(api, "pause_workspace_session", row, bus) if surface == "tool" else _rest(api, "pause", row)))

    return pause


async def _replied(process: _Process, row: WorkspaceSession) -> list:
    return await process.sp.get_event_store().read_after(0, event_type_prefix="session.replied")


@PROCESSES
@pytest.mark.parametrize("surface", ["tool", "rest"])
async def test_a_pause_that_lands_mid_turn_rests_the_session_paused_when_the_turn_ends(surface, two_processes, tmp_path, fake_event_bus):
    """The turn is not cut short (one model call, its answer stands: the turn is marked completed and its reply announced), and the session then
    reads PAUSED with ``pause_requested`` still set; nothing re-arms it. On main it rested WAITING with the flag."""
    async with asyncio.timeout(BOUND_S):
        api, worker, row, closers = await _world(tmp_path, two_processes=two_processes, turn_no=1)
        try:
            answers: list = []
            llm = _ActingLLM(_pauser(api, row, surface, fake_event_bus, answers), scripts=[OK])

            await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))

            assert answers[0][0] == "ok", answers
            after = await worker.sessions.get(row.id)
            assert (after.status, after.pause_requested, after.ended_reason, len(llm.calls)) == (SessionStatus.PAUSED, True, None, 1)
            assert (after.turn_status, after.completed_turn_no) == ("idle", 1), "the turn's own end was not recorded"
            assert len(await _replied(worker, row)) == 1, "the turn's reply was not announced"
            assert (worker.scheduler.enqueued, worker.engine.upserts) == ([], []), "something re-armed the paused session"
        finally:
            await _close(closers)


@PROCESSES
async def test_a_resume_after_a_mid_turn_pause_runs_the_next_turn(two_processes, tmp_path, fake_event_bus):
    """The way back is the resume: RUNNING, the flag cleared, the claim armed, and the next turn runs and rests WAITING."""
    async with asyncio.timeout(BOUND_S):
        api, worker, row, closers = await _world(tmp_path, two_processes=two_processes, turn_no=1)
        try:
            answers: list = []
            llm = _ActingLLM(_pauser(api, row, "tool", fake_event_bus, answers), scripts=[OK])
            await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))
            assert (await worker.sessions.get(row.id)).status == SessionStatus.PAUSED

            kind, body = await _tool(api, "resume_workspace_session", row, fake_event_bus)
            assert (kind, body["status"], body["pause_requested"]) == ("ok", "running", False), body
            assert (api.scheduler.enqueued, api.engine.upserts) == ([row.id], [row.id])

            # the claim's release bumps turn_no after a turn that ran (the adapter's on_release, which this test does not run)
            assert await worker.sessions.patch_if(row.id, {"turn_no": 2}, where={"turn_no": [1]}) is not None
            again = _FakeLLM(scripts=[OK])
            await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(again, fake_event_bus))
            after = await worker.sessions.get(row.id)
            assert (after.status, after.pause_requested, len(again.calls)) == (SessionStatus.WAITING, False, 1)
        finally:
            await _close(closers)


@PROCESSES
async def test_an_autonomous_session_paused_mid_turn_still_ends_when_its_turn_ends_cleanly(two_processes, tmp_path, fake_event_bus):
    """The lead's question on the ruling, decided from the code: a requested pause does not win over a clean autonomous end. The end is the turn's
    own outcome (``_post_turn_status``: a self-driving session has nobody to resume it, and a graph session that rested would run its graph from
    Begin again on a resume), and a pause acts between turns: an ended session runs no further turn, which is what the pause asked for."""
    async with asyncio.timeout(BOUND_S):
        api, worker, row, closers = await _world(tmp_path, two_processes=two_processes, turn_no=1, autonomous=True)
        try:
            answers: list = []
            llm = _ActingLLM(_pauser(api, row, "tool", fake_event_bus, answers), scripts=[OK])

            await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))

            after = await worker.sessions.get(row.id)
            assert (after.status, after.ended_reason, len(llm.calls)) == (SessionStatus.ENDED, "completed", 1)
        finally:
            await _close(closers)


@PROCESSES
async def test_a_stop_and_a_pause_that_land_mid_turn_rest_the_session_paused(two_processes, tmp_path, fake_event_bus):
    """A pause and a Stop that both land while the turn runs (the docs' way to stop a session now and keep it stopped): the Stop cuts the turn short
    and its exit, which rests a stopped session, rests it PAUSED because the pause is on the row. On main it rested WAITING with the flag. (The pause
    goes first here: the Stop cancels the model call this test runs its calls in.)"""
    async with asyncio.timeout(BOUND_S):
        api, worker, row, closers = await _world(tmp_path, two_processes=two_processes, turn_no=1)
        try:
            answers: list = []

            async def pause_and_stop() -> None:
                answers.append(await _tool(api, "pause_workspace_session", row, fake_event_bus))
                answers.append(await _tool(api, "interrupt_workspace_session", row, fake_event_bus))

            llm = _ActingLLM(pause_and_stop, then="hang", scripts=[OK])
            await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))

            assert answers[0][0] == "ok", answers
            after = await worker.sessions.get(row.id)
            assert (after.status, after.pause_requested, after.ended_reason, after.interrupt_requested) == (SessionStatus.PAUSED, True, None, False)
            assert (after.turn_status, len(llm.calls)) == ("idle", 1)
            assert await _replied(worker, row) == [], "the turn took the clean end, not the Stop's exit"
        finally:
            await _close(closers)


@PROCESSES
async def test_a_turn_that_fails_after_a_mid_turn_pause_rests_paused(two_processes, tmp_path, fake_event_bus):
    """A model call that fails in a way an interactive session rests after (a rate limit): the session rests, and with a pause on the row it rests
    PAUSED; the row still says the turn failed. On main it rested WAITING with the flag."""
    async with asyncio.timeout(BOUND_S):
        api, worker, row, closers = await _world(tmp_path, two_processes=two_processes, turn_no=1)
        try:
            answers: list = []
            llm = _ActingLLM(_pauser(api, row, "tool", fake_event_bus, answers), then="raise", scripts=[OK])

            await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))

            after = await worker.sessions.get(row.id)
            assert (after.status, after.pause_requested, after.ended_reason) == (SessionStatus.PAUSED, True, None)
            assert after.last_turn_error is not None and after.last_turn_error.code == "rate_limit"
        finally:
            await _close(closers)


@PROCESSES
async def test_a_turn_without_a_pause_still_rests_waiting(two_processes, tmp_path, fake_event_bus):
    """The control: no pause, the clean end rests WAITING as before."""
    async with asyncio.timeout(BOUND_S):
        _, worker, row, closers = await _world(tmp_path, two_processes=two_processes, turn_no=1)
        try:
            llm = _FakeLLM(scripts=[OK])
            await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))
            after = await worker.sessions.get(row.id)
            assert (after.status, after.pause_requested, after.completed_turn_no) == (SessionStatus.WAITING, False, 1)
        finally:
            await _close(closers)


@PROCESSES
async def test_a_pause_written_as_a_status_while_the_turn_ran_is_not_undone_by_the_turn_s_end(two_processes, tmp_path, fake_event_bus):
    """A turn claimed off a WAITING row (the status a rested session keeps until a wake moves it) and paused meanwhile: the pause found the row
    WAITING and wrote PAUSED at once, with no flag. The turn's end used to write WAITING over it, and the pause was gone without a resume."""
    async with asyncio.timeout(BOUND_S):
        api, worker, row, closers = await _world(tmp_path, two_processes=two_processes, status=SessionStatus.WAITING, turn_no=1)
        try:
            answers: list = []
            llm = _ActingLLM(_pauser(api, row, "rest", fake_event_bus, answers), scripts=[OK])

            await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))

            assert answers[0][0] == "ok", answers
            after = await worker.sessions.get(row.id)
            assert (after.status, after.ended_reason, len(llm.calls)) == (SessionStatus.PAUSED, None, 1)
        finally:
            await _close(closers)


def _deliver_kwargs(process: _Process, bus) -> dict[str, Any]:
    return {
        "storage_provider": process.sp, "scheduler": process.scheduler, "claim_engine": process.engine,
        "workspace_registry": _Registry(process.workspace), "event_bus": bus,
    }


@PROCESSES
async def test_after_a_mid_turn_pause_a_machine_message_is_held_and_a_persons_message_resumes_the_session(two_processes, tmp_path, fake_event_bus):
    """The wake path reads the rest: a message no person wrote (a trigger's, another session's) is held while the session is paused, and a person's
    message resumes it, as it resumes any paused session. On main the row rested WAITING with the flag: the machine message was held, but then the
    person's message queued behind it (a WAITING row with messages waiting is busy) and the session stayed paused."""
    async with asyncio.timeout(BOUND_S):
        api, worker, row, closers = await _world(tmp_path, two_processes=two_processes, turn_no=1)
        try:
            answers: list = []
            llm = _ActingLLM(_pauser(api, row, "tool", fake_event_bus, answers), scripts=[OK])
            await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))

            held = await deliver_steer(
                session_id=row.id, text="from a trigger", parallelism="queue", human_intent=False, **_deliver_kwargs(api, fake_event_bus),
            )
            assert held.outcome == DELIVERED_QUEUED
            rested = await api.sessions.get(row.id)
            assert (rested.status, rested.pause_requested) == (SessionStatus.PAUSED, True), "the session did not rest paused, or a machine message lifted it"

            person = await deliver_steer(
                session_id=row.id, text="carry on", parallelism="queue", human_intent=True, **_deliver_kwargs(api, fake_event_bus),
            )
            assert person.outcome == DELIVERED_WOKEN, person
            after = await api.sessions.get(row.id)
            assert (after.status, after.pause_requested, after.turn_status) == (SessionStatus.RUNNING, False, "claimable")
            lines = (await api.workspace.read_file(f".state/sessions/{row.id}/messages.jsonl")).decode().splitlines()
            records = [json.loads(line) for line in lines if line and "kind" in json.loads(line)]
            # the trigger's message held (queued, not written), the person's written, and the pause they lifted recorded as superseded
            assert [(r["kind"], r["payload"].get("action") or r["payload"].get("text")) for r in records] == [
                ("pause_superseded", "queued"), ("user_input", "carry on"), ("pause_superseded", "cleared"),
            ], records
        finally:
            await _close(closers)


@PROCESSES
async def test_the_workspace_session_tools_serve_a_session_paused_mid_turn_as_paused(two_processes, tmp_path, fake_event_bus):
    """The session tools serve the on-disk slot overlaid with the row (``primer.session.slot_view``). The slot still says RUNNING after a clean
    turn and is never told of a pause, so the overlay takes a PAUSED row's status as it takes a WAITING one's: before, a paused session read
    ``running`` through ``get_workspace_session`` and ``list_workspace_sessions``."""
    async with asyncio.timeout(BOUND_S):
        api, worker, row, closers = await _world(tmp_path, two_processes=two_processes, turn_no=1)
        try:
            answers: list = []
            llm = _ActingLLM(_pauser(api, row, "tool", fake_event_bus, answers), scripts=[OK])
            await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))

            kind, got = await _tool(api, "get_workspace_session", row, fake_event_bus)
            assert (kind, got["status"]) == ("ok", "paused"), got
            listed = await api.toolset(fake_event_bus).call(tool_name="list_workspace_sessions", arguments={"workspace_id": row.workspace_id})
            assert [(s["session_id"], s["status"]) for s in json.loads(listed.output)["items"] if s["session_id"] == row.id] == [(row.id, "paused")]
        finally:
            await _close(closers)
