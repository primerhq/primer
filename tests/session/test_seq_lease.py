"""T55 (ticket 01a16c3a N6): a record written while a turn runs never repeats a seq the turn's writer owns.

The turn's writer holds a LEASE on the row: it moves ``last_seq`` ahead of the seqs it has numbered, in blocks, so a writer that
reserves from the row (an advisory record, an attachment steer) takes a seq past every one the turn will use. A turn with no
concurrent writer numbers its records exactly as before, and gives the lease back when it settles.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding, SessionMessageKind, SessionMessageRecord, SessionStatus, WorkspaceSession,
)
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.pending_messages import store_pending_steer
from primer.session.persistence import WorkspaceMessageWriter
from primer.session.seq_reservation import reserve_next_seq
from tests.conftest import _FakeStorageProvider


class _WS:
    def __init__(self):
        self.data = defaultdict(bytes)

    async def append_message_line(self, sid, line):
        self.data[sid] += line

    async def get_session(self, sid):
        return None


class _Reg:
    def __init__(self, ws):
        self.ws = ws

    async def get_workspace(self, wid):
        return self.ws


class _Sched:
    async def enqueue(self, sid):
        ...


class _Eng:
    async def upsert(self, *a, **k):
        ...


def _seqs(ws, sid):
    return [json.loads(l)["seq"] for l in ws.data[sid].decode().splitlines() if l.strip()]


def _rec(kind=SessionMessageKind.LLM_CALL):
    return SessionMessageRecord(seq=1, kind=kind, payload={}, created_at=datetime.now(timezone.utc))


async def _running(sp, sid, last_seq=5):
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id=sid, workspace_id="w", binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc), turn_status="running", last_seq=last_seq,
    ))
    return sessions


def _turn_writer(ws, sessions, sid, start_seq=5):
    return WorkspaceMessageWriter(workspace_io=ws, session_id=sid, start_seq=start_seq, lease=sessions)


def _deps(sp, ws):
    return SessionWakeDeps(storage_provider=sp, scheduler=_Sched(), claim_engine=_Eng(), workspace_registry=_Reg(ws), event_bus=None)


@pytest.mark.asyncio
async def test_no_concurrent_writer_numbers_records_as_before_and_settles_the_row():
    sp, ws, sid = _FakeStorageProvider(), _WS(), "s0"
    sessions = await _running(sp, sid)
    writer = _turn_writer(ws, sessions, sid)
    got = [await writer.append(_rec()) for _ in range(3)]
    await writer.flush()
    assert got == [6, 7, 8] and _seqs(ws, sid) == [6, 7, 8]
    assert (await sessions.get(sid)).last_seq > 8           # the lease is held while the turn runs
    await writer.settle()
    assert (await sessions.get(sid)).last_seq == 8          # and given back: the next writer seeds exactly as before


@pytest.mark.asyncio
async def test_an_advisory_record_between_two_turn_records_repeats_nothing():
    sp, ws, sid = _FakeStorageProvider(), _WS(), "s1"
    sessions = await _running(sp, sid)
    writer = _turn_writer(ws, sessions, sid)
    await writer.append(_rec())
    advisory = await reserve_next_seq(sessions, sid)
    other = WorkspaceMessageWriter(workspace_io=ws, session_id=sid, start_seq=advisory - 1)
    await other.append(_rec(SessionMessageKind.PAUSE_SUPERSEDED))
    await other.flush()
    for _ in range(3):
        await writer.append(_rec())
    await writer.flush()
    await writer.settle()
    nums = _seqs(ws, sid)
    assert len(nums) == len(set(nums)) == 5
    assert (await sessions.get(sid)).last_seq >= max(nums)  # settling never lowers the row under a seq somebody took


@pytest.mark.asyncio
async def test_a_turn_longer_than_one_block_still_never_repeats_a_seq():
    sp, ws, sid = _FakeStorageProvider(), _WS(), "s2"
    sessions = await _running(sp, sid)
    writer = _turn_writer(ws, sessions, sid)
    for i in range(300):
        await writer.append(_rec())
        if i % 40 == 0:
            n = await reserve_next_seq(sessions, sid)
            other = WorkspaceMessageWriter(workspace_io=ws, session_id=sid, start_seq=n - 1)
            await other.append(_rec(SessionMessageKind.PAUSE_SUPERSEDED))
            await other.flush()
    await writer.flush()
    nums = _seqs(ws, sid)
    assert len(nums) == len(set(nums)) == 300 + 8


@pytest.mark.asyncio
async def test_pause_superseded_record_during_a_running_turn_repeats_no_seq():
    sp, ws, sid = _FakeStorageProvider(), _WS(), "s3"
    sessions = await _running(sp, sid)
    writer = _turn_writer(ws, sessions, sid)
    for _ in range(2):
        await writer.append(_rec())
    await writer.flush()
    await sessions.patch_if(sid, {"pause_requested": True}, where={"workspace_id": ["w"]})
    await wake_session(workspace_id="w", session_id=sid, instruction="from a trigger", human_intent=False, deps=_deps(sp, ws))
    await writer.append(_rec())
    await writer.flush()
    nums = _seqs(ws, sid)
    assert len(nums) == len(set(nums))


@pytest.mark.asyncio
async def test_dropped_pending_record_during_a_running_turn_repeats_no_seq():
    sp, ws, sid = _FakeStorageProvider(), _WS(), "s4"
    sessions = await _running(sp, sid)
    writer = _turn_writer(ws, sessions, sid)
    for _ in range(2):
        await writer.append(_rec())
    await writer.flush()
    row = await sessions.get(sid)
    for i in range(51):
        await store_pending_steer(storage_provider=sp, session=row, text=f"m{i}", workspace_registry=_Reg(ws))
    await writer.append(_rec())
    await writer.flush()
    nums = _seqs(ws, sid)
    assert len(nums) == len(set(nums))


@pytest.mark.asyncio
async def test_steer_with_an_attachment_during_a_running_turn_repeats_no_seq():
    from primer.model.chat import TextPart
    sp, ws, sid = _FakeStorageProvider(), _WS(), "s5"
    sessions = await _running(sp, sid)
    writer = _turn_writer(ws, sessions, sid)
    for _ in range(2):
        await writer.append(_rec())
    await writer.flush()
    await wake_session(workspace_id="w", session_id=sid, instruction="look at this", human_intent=True,
                       extra_parts=[TextPart(text="<attachment>")], extra_payload={"attachments": ["a.png"]}, deps=_deps(sp, ws))
    await writer.append(_rec())
    await writer.flush()
    nums = _seqs(ws, sid)
    assert len(nums) == len(set(nums))
