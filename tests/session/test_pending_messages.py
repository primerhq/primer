"""Deferred-steer queue (S1 P1, plan Task 4).

Spec: docs/superpowers/ux-revamp/02-s1-design.md sections 3 and 4
(M5 routing rule). asyncio_mode=auto, so async tests need no marker.
"""

from datetime import UTC, datetime

from primer.model.workspace_session import (
    AgentSessionBinding,
    PendingSessionMessage,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.pending_messages import (
    realize_next_pending,
    store_pending_steer,
)
from tests.conftest import _InMemoryStorage


class _PendingStorage:
    def __init__(self):
        self.rows: dict[str, PendingSessionMessage] = {}

    async def create(self, row):
        self.rows[row.id] = row
        return row

    async def delete(self, rid):
        self.rows.pop(rid, None)

    async def find(self, predicate, page, *, order_by=None):
        items = sorted(self.rows.values(), key=lambda r: (r.enqueued_at, r.id))

        class _P:
            pass

        p = _P()
        p.items = items[: page.length]
        return p


class _SP:
    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

    def __init__(self):
        self.pending = _PendingStorage()
        # The cap's announcement reserves its seq on the session row (01a11cd8), so the provider also serves the session storage.
        self.sessions = _InMemoryStorage(WorkspaceSession)

    def get_storage(self, cls):
        assert cls in (PendingSessionMessage, WorkspaceSession)
        return self.pending if cls is PendingSessionMessage else self.sessions


def _row(session_id: str = "sess-1") -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id,
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC),
    )


async def test_store_creates_seqless_row():
    sp = _SP()
    row = await store_pending_steer(
        storage_provider=sp,
        session=_row("sess-1"),
        text="later please",
        workspace_registry=None,
    )
    assert row.session_id == "sess-1"
    assert row.parts == [{"type": "text", "text": "later please"}]
    assert row.id in sp.pending.rows


async def test_realize_takes_oldest_single_row_and_wakes(monkeypatch):
    """Exactly one row per checkpoint keeps user_input:terminal 1:1."""
    sp = _SP()
    await store_pending_steer(
        storage_provider=sp, session=_row("s"), text="first",
        workspace_registry=None,
    )
    await store_pending_steer(
        storage_provider=sp, session=_row("s"), text="second",
        workspace_registry=None,
    )

    woken = []

    async def _fake_wake(**kw):
        woken.append(kw["instruction"])

    monkeypatch.setattr(
        "primer.session.pending_messages.wake_session",
        _fake_wake,
    )
    did = await realize_next_pending(
        storage_provider=sp,
        workspace_id="ws-1",
        session_id="s",
        wake_deps=object(),
    )
    assert did is True
    assert woken == ["first"]
    assert len(sp.pending.rows) == 1  # second still queued


async def test_realize_empty_returns_false():
    sp = _SP()
    did = await realize_next_pending(
        storage_provider=sp,
        workspace_id="ws-1",
        session_id="s",
        wake_deps=object(),
    )
    assert did is False


async def test_textless_row_is_reaped_without_waking(monkeypatch):
    """A parts-less entry must not wake a turn with an empty instruction."""
    sp = _SP()
    row = await store_pending_steer(
        storage_provider=sp,
        session=_row("s"),
        text="x",
        workspace_registry=None,
    )
    sp.pending.rows[row.id] = row.model_copy(update={"parts": []})

    woken = []

    async def _fake_wake(**kw):
        woken.append(kw["instruction"])

    monkeypatch.setattr(
        "primer.session.pending_messages.wake_session",
        _fake_wake,
    )
    did = await realize_next_pending(
        storage_provider=sp,
        workspace_id="ws-1",
        session_id="s",
        wake_deps=object(),
    )
    assert did is False


class _FakeWorkspace:
    def __init__(self) -> None:
        self.message_lines: list[bytes] = []

    async def append_message_line(self, session_id, line):
        self.message_lines.append(line)


class _FakeRegistry:
    def __init__(self, ws) -> None:
        self._ws = ws

    async def get_workspace(self, wid):
        return self._ws


def _decode_records(ws):
    import json

    records = []
    for blob in ws.message_lines:
        for line in blob.decode().splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


async def test_under_cap_drops_nothing():
    sp = _SP()
    ws = _FakeWorkspace()
    registry = _FakeRegistry(ws)
    for i in range(10):
        await store_pending_steer(
            storage_provider=sp, session=_row("s"), text=f"msg-{i}",
            workspace_registry=registry,
        )
    assert len(sp.pending.rows) == 10
    assert ws.message_lines == []


async def test_over_cap_drops_oldest_and_records_it():
    """01a08c08 ruling: bounded queue, drop oldest, record every drop.

    Seeds rows DIRECTLY with explicit, strictly-increasing enqueued_at
    timestamps (rather than via _MAX_PENDING_PER_SESSION rapid-fire calls
    to store_pending_steer) so the test's own successive-call timing can
    never collide with real wall-clock resolution and scramble which rows
    count as "oldest" -- that would make the test itself flaky, not just
    the code under test.
    """
    from datetime import timedelta

    from primer.session.pending_messages import _MAX_PENDING_PER_SESSION

    sp = _SP()
    ws = _FakeWorkspace()
    registry = _FakeRegistry(ws)
    session = _row("s")
    await sp.sessions.create(session)
    # Seeded rows are all placed in the past, so the "fresh" row stored via
    # store_pending_steer below (a real datetime.now() call) reliably sorts
    # AFTER every one of them -- anchoring base at "now" would instead let
    # a fast test loop's real timestamp land BEFORE most seeded offsets,
    # making "fresh" sort near the front and scrambling which rows this
    # test can honestly claim are "the oldest".
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(_MAX_PENDING_PER_SESSION):
        ts = base + timedelta(seconds=i)
        row = PendingSessionMessage(
            id=f"s:pending:{ts.isoformat()}:{i:08x}",
            session_id="s",
            parts=[{"type": "text", "text": f"msg-{i}"}],
            enqueued_at=ts,
            created_at=ts,
        )
        sp.pending.rows[row.id] = row
    assert len(sp.pending.rows) == _MAX_PENDING_PER_SESSION

    # The cap-plus-one-th store pushes it over: the single oldest seeded
    # row must be dropped and recorded.
    await store_pending_steer(
        storage_provider=sp, session=session, text="fresh",
        workspace_registry=registry,
    )
    assert len(sp.pending.rows) == _MAX_PENDING_PER_SESSION
    surviving_texts = {
        p["text"] for row in sp.pending.rows.values() for p in row.parts
    }
    assert "msg-0" not in surviving_texts, "the single oldest row must be dropped"
    assert "msg-1" in surviving_texts
    assert "fresh" in surviving_texts

    # The drop is durably recorded, naming what was dropped and why.
    records = _decode_records(ws)
    dropped_records = [
        r for r in records if r["kind"] == "pause_superseded"
        and r["payload"]["action"] == "dropped"
    ]
    assert len(dropped_records) == 1
    assert dropped_records[0]["payload"]["text"] == "msg-0"
    assert str(_MAX_PENDING_PER_SESSION) in dropped_records[0]["payload"]["reason"]


async def test_over_cap_by_several_drops_all_of_the_oldest():
    """Seeding well past the cap in one shot (e.g. a burst) must drop
    ALL of the excess, oldest-first, not just one."""
    from datetime import timedelta

    from primer.session.pending_messages import _MAX_PENDING_PER_SESSION

    sp = _SP()
    ws = _FakeWorkspace()
    registry = _FakeRegistry(ws)
    session = _row("s")
    await sp.sessions.create(session)
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(_MAX_PENDING_PER_SESSION + 4):
        ts = base + timedelta(seconds=i)
        row = PendingSessionMessage(
            id=f"s:pending:{ts.isoformat()}:{i:08x}",
            session_id="s",
            parts=[{"type": "text", "text": f"msg-{i}"}],
            enqueued_at=ts,
            created_at=ts,
        )
        sp.pending.rows[row.id] = row

    await store_pending_steer(
        storage_provider=sp, session=session, text="fresh",
        workspace_registry=registry,
    )
    assert len(sp.pending.rows) == _MAX_PENDING_PER_SESSION
    surviving_texts = {
        p["text"] for row in sp.pending.rows.values() for p in row.parts
    }
    for i in range(5):
        assert f"msg-{i}" not in surviving_texts
    assert "fresh" in surviving_texts

    records = _decode_records(ws)
    dropped_records = [
        r for r in records if r["kind"] == "pause_superseded"
        and r["payload"]["action"] == "dropped"
    ]
    assert {r["payload"]["text"] for r in dropped_records} == {
        f"msg-{i}" for i in range(5)
    }


async def test_missing_workspace_registry_skips_the_record_not_the_drop():
    """A None workspace_registry (some test/edge-case callers) must not
    crash the drop itself -- the cap is enforced either way, the
    announcement is just unreachable without a workspace to write to."""
    from primer.session.pending_messages import _MAX_PENDING_PER_SESSION

    sp = _SP()
    session = _row("s")
    await sp.sessions.create(session)
    for i in range(_MAX_PENDING_PER_SESSION + 1):
        await store_pending_steer(
            storage_provider=sp, session=session, text=f"msg-{i}",
            workspace_registry=None,
        )
    assert len(sp.pending.rows) == _MAX_PENDING_PER_SESSION
