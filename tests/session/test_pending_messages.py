"""Deferred-steer queue (S1 P1, plan Task 4).

Spec: docs/superpowers/ux-revamp/02-s1-design.md sections 3 and 4
(M5 routing rule). asyncio_mode=auto, so async tests need no marker.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding,
    PeerAttribution,
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
        p.items = items[page.offset : page.offset + page.length]
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


# ---- a message from another session never costs a person's queued steer (pivot P3 slice B, lead ruling R1) ---------------------------
#
# The cap is 50 messages per session. A peer message (``attribution["peer"]``) is refused when 50 already wait, and the cap drops the oldest
# peer message before it drops anything else, so a peer can never push a person's steer out, even when two senders pass the check at once.


def _peer(hops: int = 1) -> dict:
    return PeerAttribution(
        from_session_id="ses-a", from_workspace_id="ws-1", from_agent_id="planner", hops=hops,
    ).as_attribution()


def _peer_text(label: str) -> str:
    return f"[from session ses-a (agent planner)]\n{label}"


def _seed(sp, rows) -> None:
    """Queue ``rows``, oldest first, as ``(text, attribution)`` pairs, directly and at strictly increasing times in the past, as the cap tests above do:
    which row is the oldest must never depend on the clock the test runs at."""
    base = datetime.now(UTC) - timedelta(hours=1)
    for i, (text, attribution) in enumerate(rows):
        ts = base + timedelta(seconds=i)
        row = PendingSessionMessage(
            id=f"s:pending:{ts.isoformat()}:{i:08x}", session_id="s", parts=[{"type": "text", "text": text}],
            attribution=attribution, enqueued_at=ts, created_at=ts,
        )
        sp.pending.rows[row.id] = row


def _queued_texts(sp) -> list[str]:
    ordered = sorted(sp.pending.rows.values(), key=lambda r: (r.enqueued_at, r.id))
    return [p["text"] for row in ordered for p in row.parts]


def _dropped(ws) -> list[dict]:
    return [
        r["payload"] for r in _decode_records(ws) if r["kind"] == "pause_superseded" and r["payload"]["action"] == "dropped"
    ]


async def _session_with(sp, rows) -> WorkspaceSession:
    session = _row("s")
    await sp.sessions.create(session)
    _seed(sp, rows)
    return session


async def test_a_persons_steer_at_the_cap_evicts_the_oldest_peer_message_not_the_oldest_row():
    from primer.session.pending_messages import _MAX_PENDING_PER_SESSION

    sp, ws = _SP(), _FakeWorkspace()
    session = await _session_with(sp, [
        (_peer_text(f"peer-{i}"), _peer()) if i in (3, 7) else (f"person-{i}", None) for i in range(_MAX_PENDING_PER_SESSION)
    ])
    await store_pending_steer(storage_provider=sp, session=session, text="fresh", workspace_registry=_FakeRegistry(ws))

    queued = _queued_texts(sp)
    assert len(queued) == _MAX_PENDING_PER_SESSION
    assert "person-0" in queued, "the cap dropped a person's oldest steer while a peer message was queued"
    assert _peer_text("peer-3") not in queued, "the oldest peer message is the one that goes"
    assert _peer_text("peer-7") in queued and "fresh" in queued
    [dropped] = _dropped(ws)
    assert dropped["text"] == _peer_text("peer-3"), "the record names the peer text"
    assert str(_MAX_PENDING_PER_SESSION) in dropped["reason"]


async def test_a_persons_steer_at_the_cap_with_no_peer_message_queued_evicts_the_oldest_row_as_before():
    """Only a ``peer`` attribution counts: a message with some other attribution is an ordinary one."""
    from primer.session.pending_messages import _MAX_PENDING_PER_SESSION

    sp, ws = _SP(), _FakeWorkspace()
    session = await _session_with(sp, [
        (f"msg-{i}", {"channel": "slack"} if i % 2 else None) for i in range(_MAX_PENDING_PER_SESSION)
    ])
    await store_pending_steer(storage_provider=sp, session=session, text="fresh", workspace_registry=_FakeRegistry(ws))

    queued = _queued_texts(sp)
    assert len(queued) == _MAX_PENDING_PER_SESSION
    assert "msg-0" not in queued and "msg-1" in queued and "fresh" in queued
    [dropped] = _dropped(ws)
    assert dropped["text"] == "msg-0"


async def test_the_cap_drops_every_queued_peer_message_before_it_touches_a_persons_row():
    """More over the cap than there are peer messages: all of them go first, oldest first, and only then the oldest of the rest."""
    from primer.session.pending_messages import _MAX_PENDING_PER_SESSION

    sp, ws = _SP(), _FakeWorkspace()
    session = await _session_with(sp, [
        (_peer_text(f"peer-{i}"), _peer()) if i in (10, 30) else (f"person-{i}", None) for i in range(_MAX_PENDING_PER_SESSION + 3)
    ])
    await store_pending_steer(storage_provider=sp, session=session, text="fresh", workspace_registry=_FakeRegistry(ws))

    queued = _queued_texts(sp)
    assert len(queued) == _MAX_PENDING_PER_SESSION
    assert [d["text"] for d in _dropped(ws)] == [_peer_text("peer-10"), _peer_text("peer-30"), "person-0", "person-1"]
    assert "person-2" in queued and "fresh" in queued


async def test_a_peer_message_is_refused_at_the_cap_before_anything_is_stored():
    from primer.model.except_ import ConflictError
    from primer.session.pending_messages import _MAX_PENDING_PER_SESSION, PendingQueueFull

    sp, ws = _SP(), _FakeWorkspace()
    session = await _session_with(sp, [(f"person-{i}", None) for i in range(_MAX_PENDING_PER_SESSION)])
    before = dict(sp.pending.rows)
    with pytest.raises(PendingQueueFull) as info:
        await store_pending_steer(
            storage_provider=sp, session=session, text=_peer_text("one more"), workspace_registry=_FakeRegistry(ws), attribution=_peer(),
        )
    assert isinstance(info.value, ConflictError)
    assert sp.pending.rows == before, "the refused message changed the queue"
    assert ws.message_lines == [] and (await sp.sessions.get("s")).last_seq == session.last_seq, "a refusal records nothing and reserves no seq"


async def test_a_peer_message_is_refused_at_the_cap_whoever_filled_it():
    from primer.session.pending_messages import _MAX_PENDING_PER_SESSION, PendingQueueFull

    sp, ws = _SP(), _FakeWorkspace()
    session = await _session_with(sp, [(_peer_text(f"peer-{i}"), _peer()) for i in range(_MAX_PENDING_PER_SESSION)])
    with pytest.raises(PendingQueueFull):
        await store_pending_steer(
            storage_provider=sp, session=session, text=_peer_text("one more"), workspace_registry=_FakeRegistry(ws), attribution=_peer(),
        )
    assert len(sp.pending.rows) == _MAX_PENDING_PER_SESSION and _dropped(ws) == []


async def test_a_peer_message_is_stored_when_the_queue_is_one_short_of_the_cap():
    from primer.session.pending_messages import _MAX_PENDING_PER_SESSION

    sp, ws = _SP(), _FakeWorkspace()
    session = await _session_with(sp, [(f"person-{i}", None) for i in range(_MAX_PENDING_PER_SESSION - 1)])
    await store_pending_steer(
        storage_provider=sp, session=session, text=_peer_text("last one"), workspace_registry=_FakeRegistry(ws), attribution=_peer(),
    )
    assert len(sp.pending.rows) == _MAX_PENDING_PER_SESSION
    assert "person-0" in _queued_texts(sp) and _dropped(ws) == []


# ---- a queued peer message that the session refuses is dropped, never lost silently, and never blocks the rows behind it ----------------


def _wake_deps(ws):
    return SimpleNamespace(workspace_registry=_FakeRegistry(ws), event_bus=None)


async def test_a_peer_row_the_wake_refuses_as_cancelled_is_dropped_with_a_record_and_the_next_row_is_realized(monkeypatch):
    from primer.session.enqueue import OnlyAPersonReopens

    sp, ws = _SP(), _FakeWorkspace()
    await _session_with(sp, [(_peer_text("p1"), _peer()), ("q", None), (_peer_text("p2"), _peer())])
    woken = []

    async def _wake(**kw):
        if kw["peer"] is not None:
            raise OnlyAPersonReopens("cancelled")
        woken.append(kw["instruction"])

    monkeypatch.setattr("primer.session.pending_messages.wake_session", _wake)
    did = await realize_next_pending(storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws))

    assert did is True and woken == ["q"], "the person's steer behind the dropped peer message was blocked"
    assert _queued_texts(sp) == [_peer_text("p2")], "one row is realized per checkpoint: what follows it waits"
    [dropped] = _dropped(ws)
    assert dropped["text"] == _peer_text("p1") and "cancelled" in dropped["reason"]


async def test_when_the_wake_refuses_every_queued_peer_row_nothing_is_realized_and_each_drop_is_recorded(monkeypatch):
    from primer.session.enqueue import OnlyAPersonReopens

    sp, ws = _SP(), _FakeWorkspace()
    await _session_with(sp, [(_peer_text("p1"), _peer()), (_peer_text("p2"), _peer())])

    async def _wake(**kw):
        raise OnlyAPersonReopens("cancelled")

    monkeypatch.setattr("primer.session.pending_messages.wake_session", _wake)
    did = await realize_next_pending(storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws))

    assert did is False and sp.pending.rows == {}
    assert [d["text"] for d in _dropped(ws)] == [_peer_text("p1"), _peer_text("p2")]


async def test_a_peer_row_behind_a_pause_stays_in_its_place_even_when_the_queue_is_full():
    """The drain replays a queued message as a non-human wake, and a pause that landed since holds it. The hold used to store it AGAIN, which a full
    queue refused, so the message was dropped on the record. Now it stays where it is (it is already queued, and its place is its order: audit
    G7-03 (b)): nothing is stored again, so nothing is refused, dropped or written, and the queue is unchanged."""
    from primer.session.enqueue import SessionWakeDeps
    from primer.session.pending_messages import _MAX_PENDING_PER_SESSION

    sp, ws = _SP(), _FakeWorkspace()
    session = await _session_with(sp, [(_peer_text(f"p{i}"), _peer()) for i in range(_MAX_PENDING_PER_SESSION)])
    await sp.sessions.update(session.model_copy(update={"status": SessionStatus.PAUSED}))
    before = sorted(sp.pending.rows)

    did = await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s",
        wake_deps=SessionWakeDeps(storage_provider=sp, scheduler=None, claim_engine=None, workspace_registry=_FakeRegistry(ws)),
    )

    assert did is False
    assert sorted(sp.pending.rows) == before
    assert _decode_records(ws) == []
    assert (await sp.sessions.get("s")).status == SessionStatus.PAUSED
