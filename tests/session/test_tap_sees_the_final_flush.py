"""The tap announces what a turn's final flush made durable (console review 2026-10-08, C-008 and C-011).

The message writer buffers records (100 ms or 16 KB, and the age flush happens only on the NEXT append), and the live tap reads
the DURABLE ``messages.jsonl`` when a ``session:{sid}:tick`` wakes it. Dispatch ticked right after each ``append``, so for the
last records of a turn the tick went out while they were still in the buffer; the clean-completion path then flushed and
published nothing. The tap reader had already looked at the file for the last tick and had no reason to look again, so the
turn's final assistant text and its DONE record stayed invisible to every live client until some LATER tick (the next
message, a status change) came. The console showed a finished turn as "still thinking" or "no reply" until a reload.

These run the real dispatch with the real writer over a real ``LocalWorkspace``, and feed the real tap stream
(``_stream_tap``) from the real router: the reader is woken ONLY by ticks, exactly as in production. The invariant is
"every record the log holds when the turn has ended was announced to the tap", whichever way the turn ended.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from primer.api.routers.tap import _stream_tap
from primer.bus.in_memory import InMemoryEventBus
from primer.int.claim import ClaimKind, Lease
from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.tap.cursor import TapCursor
from primer.tap.router import WorkspaceTapRouter
from primer.tap.selector import TapSelector
from primer.worker.io_shim import _WorkspaceIOShim
from tests._support.off_golden import open_session
from tests.conftest import _FakeStorageProvider

WORKSPACE_ID = "w1"
_SETTLE_S = 3.0          # how long the tap gets to deliver what it was told about; a green run returns at once
_WRITE_LATENCY_S = 0.05  # one workspace write; a docker exec or a k8s runtime call is never instant


class _Registry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str):
        return self._workspace if workspace_id == WORKSPACE_ID else None


class _SlowWorkspaceWrites(_WorkspaceIOShim):
    """The production write path with a realistic cost per append.

    A local file append finishes before the tap's queue hops are even scheduled, which would hide the race (the reader would
    always look AFTER the flush). A remote workspace takes real time to take a write, and the tap reads in that window.
    """

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        await asyncio.sleep(_WRITE_LATENCY_S)
        await super().append_message_line(session_id, line)


class _Clean:
    """A turn that streams some text and stops (several deltas, so the records are written one after another)."""

    last_done_reason = "stop"

    async def invoke(self, messages: list[Any], **kwargs: Any):
        for i in range(3):
            yield TextDelta(text=f"part {i} ", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


class _CancelledMidTurn:
    """A turn that streams text and then waits (a slow tool or model call) until the test lands a hard Cancel."""

    def __init__(self) -> None:
        self.resume = asyncio.Event()

    async def invoke(self, messages: list[Any], **kwargs: Any):
        yield TextDelta(text="working on it", index=0)
        await self.resume.wait()
        yield TextDelta(text="never persisted", index=0)


class _FailsAfterText:
    """A turn whose model stream dies after it produced text."""

    async def invoke(self, messages: list[Any], **kwargs: Any):
        yield TextDelta(text="half an answer", index=0)
        raise RuntimeError("the model connection dropped")


class _ParksAfterText:
    """A turn that streams text and then yields to the worker (an ask_user park)."""

    async def invoke(self, messages: list[Any], **kwargs: Any):
        yield TextDelta(text="I need to ask you something", index=0)
        raise YieldToWorker(
            Yielded(tool_name="ask_user", event_key="ask_user:s1:tc-1", resume_metadata={"prompt": "which one?"}),
            tool_call_id="tc-1", llm_messages=[],
        )


def _lease(session_id: str) -> Lease:
    now = datetime.now(UTC)
    return Lease(kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1", claimed_at=now, expires_at=now,
                 attempt_count=1, last_error=None)


async def _wait_for_subscriber(router: WorkspaceTapRouter) -> None:
    """A tick published before the tap subscribed is dropped (ticks are advisory); poll the router's real registry."""
    async def _poll() -> None:
        while not router._subs.get(WORKSPACE_ID):
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=5)


def _durable_seqs(workspace, session_id: str) -> list[int]:
    path = workspace.root / workspace.template.state_path / "sessions" / session_id / "messages.jsonl"
    seqs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        # The log also holds the session's seed Message lines (no seq); the tap reads records only, and skips markers.
        if isinstance(obj.get("seq"), int) and obj.get("kind") != "compaction_marker":
            seqs.append(obj["seq"])
    return seqs


async def _run_turn_with_a_live_tap(
    tmp_path, executor, *, while_running=None,
) -> tuple[list[int], list[int], list[str]]:
    """Run one turn while a tap stream (woken only by ticks) follows the workspace.

    ``while_running(sessions, bus, session_id)``, when given, runs concurrently with the turn (a Cancel landing mid-turn).

    Returns ``(durable_seqs, announced_seqs, announced_classes)``: what the log holds once the turn is over, and what the tap
    delivered to its client after being given up to ``_SETTLE_S`` seconds to catch up.
    """
    backend, workspace, session = await open_session(tmp_path)
    bus = InMemoryEventBus()
    await bus.initialize()
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    router = WorkspaceTapRouter(bus, sessions)
    await router.start()
    announced: list[dict] = []
    stream = _stream_tap(
        router=router, sessions_storage=sessions, workspace_io=workspace, workspace_id=WORKSPACE_ID,
        selector=TapSelector(), cursor=TapCursor(seqs={}, known_as_of=datetime.now(UTC)), had_cursor=True,
    )

    async def _collect() -> None:
        async for frame in stream:
            if frame.startswith("id:"):
                announced.append(json.loads(frame.split("data: ", 1)[1]))

    collector = None
    try:
        await sessions.create(WorkspaceSession(
            id=session.session_id, workspace_id=WORKSPACE_ID, binding=AgentSessionBinding(agent_id="ag1"),
            status=SessionStatus.RUNNING, created_at=datetime.now(UTC), turn_status="running",
        ))
        shim = _SlowWorkspaceWrites(_Registry(workspace))
        shim.register_session(session.session_id, WORKSPACE_ID)
        collector = asyncio.create_task(_collect())
        await _wait_for_subscriber(router)

        async def build(_session):
            return executor

        turn = run_one_session_turn(_lease(session.session_id), SessionDispatchDeps(
            storage_provider=sp, workspace_io=shim, event_bus=bus, build_executor=build,
        ))
        if while_running is None:
            await asyncio.wait_for(turn, timeout=15)
        else:
            await asyncio.wait_for(asyncio.gather(turn, while_running(sessions, bus, session.session_id)), timeout=15)

        durable = _durable_seqs(workspace, session.session_id)
        try:
            async with asyncio.timeout(_SETTLE_S):
                while {a["seq"] for a in announced} < set(durable):
                    await asyncio.sleep(0.02)
        except TimeoutError:
            pass
        return durable, [a["seq"] for a in announced], [a.get("class") for a in announced]
    finally:
        if collector is not None:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
        await stream.aclose()
        await router.aclose()
        await bus.aclose()
        await session.aclose()
        await backend.aclose()


def _land_a_hard_cancel(executor: _CancelledMidTurn):
    """What ``cancel_session`` does to a running turn: set the flag on the row, publish the cancel signal."""
    async def land(sessions, bus, session_id: str) -> None:
        await asyncio.sleep(0.3)                       # the executor has streamed its text and is waiting
        row = await sessions.get(session_id)
        row.cancel_requested = True
        await sessions.update(row)
        await bus.publish(f"session:{session_id}:cancel", {})
        await asyncio.sleep(0.1)
        executor.resume.set()

    return land


class TestTheTapAnnouncesWhatTheFinalFlushMadeDurable:
    async def test_a_clean_turn_announces_its_last_records(self, tmp_path) -> None:
        """The clean-completion path flushed and published nothing: the DONE record (and the final text) were never announced."""
        durable, announced, classes = await _run_turn_with_a_live_tap(tmp_path, _Clean())
        assert durable, "the turn wrote records"
        assert set(announced) == set(durable), (
            f"the log holds seqs {durable} but a tap woken only by ticks was told about {sorted(announced)}"
        )
        assert "done" in classes, "the turn's DONE record is what lets a client stop showing it as running"

    async def test_a_turn_whose_stream_fails_announces_its_error_record(self, tmp_path) -> None:
        durable, announced, classes = await _run_turn_with_a_live_tap(tmp_path, _FailsAfterText())
        assert set(announced) == set(durable), f"log {durable}, announced {sorted(announced)}"
        assert "error" in classes

    async def test_a_cancelled_turn_announces_its_cancelled_record(self, tmp_path) -> None:
        executor = _CancelledMidTurn()
        durable, announced, classes = await _run_turn_with_a_live_tap(
            tmp_path, executor, while_running=_land_a_hard_cancel(executor),
        )
        assert set(announced) == set(durable), f"log {durable}, announced {sorted(announced)}"
        assert "cancelled" in classes

    async def test_a_parked_turn_announces_its_yielded_record(self, tmp_path) -> None:
        durable, announced, classes = await _run_turn_with_a_live_tap(tmp_path, _ParksAfterText())
        assert set(announced) == set(durable), f"log {durable}, announced {sorted(announced)}"
        assert "yielded" in classes


def test_no_exit_flushes_a_turns_tail_without_a_tick_after_it() -> None:
    """The rule in ``_flush_and_tick``'s docstring, kept by the code and not by memory: dispatch flushes the writer in exactly two
    places, the helper (which ticks right after) and ``_write_cancelled_record`` (whose caller ticks after releasing the lock).
    A new exit that calls ``writer.flush()`` itself is how the clean-completion path came to publish nothing."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "primer" / "session" / "dispatch.py").read_text(encoding="utf-8")
    flushes = re.findall(r"await writer\.flush\(\)", source)
    assert len(flushes) == 2, (
        f"{len(flushes)} direct writer.flush() calls in dispatch.py; use _flush_and_tick so the tap is told what became durable"
    )
