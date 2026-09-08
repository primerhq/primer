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
