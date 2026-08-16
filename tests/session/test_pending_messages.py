"""Deferred-steer queue (S1 P1, plan Task 4).

Spec: docs/superpowers/ux-revamp/02-s1-design.md sections 3 and 4
(M5 routing rule). asyncio_mode=auto, so async tests need no marker.
"""

from primer.model.workspace_session import PendingSessionMessage
from primer.session.pending_messages import (
    realize_next_pending,
    store_pending_steer,
)


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
    def __init__(self):
        self.pending = _PendingStorage()

    def get_storage(self, cls):
        assert cls is PendingSessionMessage
        return self.pending


async def test_store_creates_seqless_row():
    sp = _SP()
    row = await store_pending_steer(
        storage_provider=sp, session_id="sess-1", text="later please",
    )
    assert row.session_id == "sess-1"
    assert row.parts == [{"type": "text", "text": "later please"}]
    assert row.id in sp.pending.rows


async def test_realize_takes_oldest_single_row_and_wakes(monkeypatch):
    """Exactly one row per checkpoint keeps user_input:terminal 1:1."""
    sp = _SP()
    await store_pending_steer(storage_provider=sp, session_id="s", text="first")
    await store_pending_steer(storage_provider=sp, session_id="s", text="second")

    woken = []

    async def _fake_wake(**kw):
        woken.append(kw["instruction"])

    monkeypatch.setattr(
        "primer.session.pending_messages.wake_session", _fake_wake,
    )
    did = await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s",
        wake_deps=object(),
    )
    assert did is True
    assert woken == ["first"]
    assert len(sp.pending.rows) == 1  # second still queued


async def test_realize_empty_returns_false():
    sp = _SP()
    did = await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s",
        wake_deps=object(),
    )
    assert did is False


async def test_textless_row_is_reaped_without_waking(monkeypatch):
    """A parts-less entry must not wake a turn with an empty instruction."""
    sp = _SP()
    row = await store_pending_steer(
        storage_provider=sp, session_id="s", text="x",
    )
    sp.pending.rows[row.id] = row.model_copy(update={"parts": []})

    woken = []

    async def _fake_wake(**kw):
        woken.append(kw["instruction"])

    monkeypatch.setattr(
        "primer.session.pending_messages.wake_session", _fake_wake,
    )
    did = await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s",
        wake_deps=object(),
    )
    assert did is False
    assert woken == []
    assert sp.pending.rows == {}  # reaped, not left to block the queue
