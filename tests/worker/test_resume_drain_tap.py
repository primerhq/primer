"""``_ResumeDrainTap`` direct unit tests (Phase 3 stage 7a, 01a0518b,
7a gate verdict item 3, condition c - the blast-radius guard).

``_ResumeDrainTap`` is SHARED infrastructure: it backs the classic
ToolCall-approval graph resume (``resume_graph_engine``), not just the
tool_wait seam. Item 3 extends its ``observe()`` method to ALSO run the
tool-dispatch seam's stash+flush step for TOOL_CALL records - these
tests pin that the extension is correctly gated: a flag-off drain (the
classic approval-gate resume, and every graph session that never opted
into tool_calls_as_claims) must behave BYTE-IDENTICALLY to before this
change - no stash, no eager flush, only the ONE flush already happening
at ``finish()``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.chat import ToolCallEnd, ToolCallStart
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.persistence import WorkspaceMessageWriter
from primer.worker.graph_resume import _ResumeDrainTap


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeWorkspaceIO:
    async def append_message_line(self, session_id: str, line: bytes) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self._storage = None
        self._event_bus = None
        self._workspace_io = _FakeWorkspaceIO()

    async def _load_workspace_for_persist(self, workspace_id: str):
        return self._workspace_io


def _session() -> WorkspaceSession:
    return WorkspaceSession(
        id="gs-tap", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=_now(), turn_no=0,
    )


async def _feed_one_tool_call(tap: _ResumeDrainTap, monkeypatch) -> int:
    """Feeds a ToolCallStart/End pair through the tap and returns the
    number of writer.flush() calls observed."""
    flush_calls = 0
    real_flush = WorkspaceMessageWriter.flush

    async def _counting_flush(self):
        nonlocal flush_calls
        flush_calls += 1
        return await real_flush(self)

    monkeypatch.setattr(WorkspaceMessageWriter, "flush", _counting_flush)

    await tap.observe(ToolCallStart(id="call-1", name="tool_a", index=0))
    await tap.observe(ToolCallEnd(id="call-1", arguments={}, index=0))
    return flush_calls


@pytest.mark.asyncio
async def test_flag_off_tap_does_not_stash_or_eager_flush(monkeypatch) -> None:
    """Blast-radius guard: the classic approval-gate resume (and every
    tool_calls_as_claims-off graph session) must see byte-identical
    behavior to before item 3's extension - no stash, no eager flush."""
    pool = _FakePool()
    tap = await _ResumeDrainTap.create(
        pool, _session(), node_tool_call_seq=None,
        tool_calls_as_claims_enabled=False,
    )
    assert tap._writer is not None

    flush_calls = await _feed_one_tool_call(tap, monkeypatch)

    assert flush_calls == 0
    assert tap.coalesce_state.tool_call_record_seq == {}
    assert tap.coalesce_state.tool_call_record_name == {}


@pytest.mark.asyncio
async def test_flag_off_is_the_default_when_unspecified(monkeypatch) -> None:
    """tool_calls_as_claims_enabled defaults to False - every EXISTING
    caller that never passes it (there was only one caller before item
    3 added the parameter) keeps today's behavior unchanged."""
    pool = _FakePool()
    tap = await _ResumeDrainTap.create(pool, _session(), node_tool_call_seq=None)

    flush_calls = await _feed_one_tool_call(tap, monkeypatch)

    assert flush_calls == 0
    assert tap.coalesce_state.tool_call_record_seq == {}


@pytest.mark.asyncio
async def test_flag_on_tap_stashes_and_eager_flushes(monkeypatch) -> None:
    """The mirror case: once armed, the tap's own TOOL_CALL handling
    matches dispatch.py's live-turn loop exactly - stash populated,
    eager flush fires - via the SAME shared helper."""
    pool = _FakePool()
    tap = await _ResumeDrainTap.create(
        pool, _session(), node_tool_call_seq=None,
        tool_calls_as_claims_enabled=True,
    )

    flush_calls = await _feed_one_tool_call(tap, monkeypatch)

    assert flush_calls == 1
    # observe() never passes node_id to translate_stream_event, so the
    # scoped id mints under the chat/workspace-surface "x" node segment
    # convention, not the raw tool-call id.
    scoped_id = "x:tool:0:1"
    assert scoped_id in tap.coalesce_state.tool_call_record_seq
    assert tap.coalesce_state.tool_call_record_name[scoped_id] == "tool_a"


# ---- a drain that loses its writer still records the seqs it spent (review of #545, B3) --------------------------------------------


class _HangingIO:
    """The workspace's runtime connection dropped: every append waits for ``release``."""

    def __init__(self) -> None:
        import asyncio

        self.release = asyncio.Event()

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        await self.release.wait()


async def _drain_world(monkeypatch):
    from primer.session import persistence
    from tests.conftest import _FakeStorageProvider

    monkeypatch.setattr(persistence, "_WRITE_TIMEOUT_S", 0.2, raising=False)
    io = _HangingIO()
    pool = _FakePool()
    pool._workspace_io = io
    pool._storage = _FakeStorageProvider()
    session = _session().model_copy(update={"last_seq": 5})
    await pool._storage.get_storage(WorkspaceSession).create(session)
    return io, pool, session


@pytest.mark.asyncio
async def test_a_tap_that_disables_itself_on_a_hung_append_still_persists_the_seqs_it_spent(monkeypatch) -> None:
    """The eager flush of a TOOL_CALL record meets the dead workspace and the writer gives up: ``observe`` disables the tap
    (``self._writer = None``) and ``finish`` used to return without writing ``last_seq``, so the row stayed at 5 while seq 6 was spent
    and the next writer reused it (duplicate seqs; the tap and ``since_seq`` hide the new record)."""
    import asyncio

    io, pool, session = await _drain_world(monkeypatch)
    tap = await _ResumeDrainTap.create(pool, session, node_tool_call_seq=None, tool_calls_as_claims_enabled=True)
    try:
        async with asyncio.timeout(5.0):
            await tap.observe(ToolCallStart(id="call-1", name="tool_a", index=0))
            await tap.observe(ToolCallEnd(id="call-1", arguments={}, index=0))
            await tap.finish()
    finally:
        io.release.set()

    row = await pool._storage.get_storage(WorkspaceSession).get(session.id)
    assert row.last_seq == 6, f"the drain spent seq 6 and the row says {row.last_seq}: the next writer reuses it"


@pytest.mark.asyncio
async def test_finish_persists_last_seq_even_when_its_own_flush_gives_up(monkeypatch) -> None:
    import asyncio

    io, pool, session = await _drain_world(monkeypatch)
    tap = await _ResumeDrainTap.create(pool, session, node_tool_call_seq=None)          # flag off: no eager flush
    try:
        async with asyncio.timeout(5.0):
            await tap.observe(ToolCallStart(id="call-1", name="tool_a", index=0))
            await tap.observe(ToolCallEnd(id="call-1", arguments={}, index=0))
            await tap.finish()                                                         # its flush meets the dead workspace
    finally:
        io.release.set()

    row = await pool._storage.get_storage(WorkspaceSession).get(session.id)
    assert row.last_seq == 6, f"the flush failed and finish skipped last_seq: the row says {row.last_seq}"
