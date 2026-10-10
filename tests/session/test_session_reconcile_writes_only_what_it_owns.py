"""The workspace-lost reconcile writes by one field-scoped, fenced ``patch_if`` per session (ticket 01a11d29, found in the #600 review).

It used to end each open session with ``session_storage.update(sess.model_copy(update=...))`` from the snapshot it read at the start. Between that read and the
write, a turn's final write, a force delete or the pool's preempt convergence could end the session (or a steer could advance ``last_seq``); the whole-document
write then put ENDED/``workspace_lost`` and every other field of the stale snapshot over it, which breaks "the first terminal reason wins"
(docs/dev/subsystems/sessions.md) and reverts fields other writers committed.

Every test here puts the reconcile behind a session storage that REFUSES every whole-document writer (and records the attempt, because the reconcile logs and
swallows what a write raises), and opens a window between the reconcile's read and its writes in which another path writes the row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from primer.model.workspace_session import (
    NON_ENDED_STATUSES,
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.workspace.session_reconcile import reconcile_sessions_to_workspace_lost

# The ONLY calls the reconcile may make on the session storage. Everything else (update, update_unless, create, upsert, delete, and any writer added later) is refused: a block-list
# would let a new whole-document writer through.
_ALLOWED = {"find", "get", "patch_if"}
_OWNED = {
    "status", "ended_reason", "ended_at", "turn_status", "turn_started_at",
    # the agent phase belongs to the turn that is gone with the workspace: the model says it is None whenever turn_status is idle
    "agent_phase", "agent_phase_turn_no", "agent_phase_stamped_at",
}


class _WindowedSessionStorage:
    """The session storage as the reconcile sees it: no whole-document writer, and a window after its read.

    ``after_find`` runs once each time a ``find`` has returned its page, before the reconcile sees the page: the moment between the snapshot and the writes at
    which another path ends or changes a row. It writes to the INNER storage (the other path is not the reconcile).
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.patches: list[tuple[str, dict, dict]] = []
        self.refused: list[str] = []
        self.after_find = None
        self.misrefuse: set[str] = set()      # ids whose patch_if answers "the fence did not match" without touching the row (a fence that misfires)
        self.get_raises = False

    async def find(self, predicate, page, **kwargs):
        out = await self.inner.find(predicate, page, **kwargs)
        if self.after_find is not None:
            await self.after_find()
        return out

    async def get(self, id, **kwargs):
        if self.get_raises:
            raise RuntimeError("storage unavailable")
        return await self.inner.get(id, **kwargs)

    async def patch_if(self, id, patch, *, where, **kwargs):
        self.patches.append((id, dict(patch), dict(where)))
        if id in self.misrefuse:
            return None
        return await self.inner.patch_if(id, patch, where=where, **kwargs)

    def __getattr__(self, name):
        if name not in _ALLOWED:
            self.refused.append(name)
            raise AssertionError(f"the reconcile must call only {sorted(_ALLOWED)} on the session storage, not {name}()")
        return getattr(self.inner, name)


class _Provider:
    def __init__(self, inner, sessions) -> None:
        self._inner = inner
        self._sessions = sessions

    def get_storage(self, model_cls):
        return self._sessions if model_cls is WorkspaceSession else self._inner.get_storage(model_cls)


def _row(session_id: str, status: SessionStatus, workspace_id: str = "w-gone", **fields) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status,
        ended_reason="completed" if status == SessionStatus.ENDED else None,
        created_at=datetime.now(timezone.utc),
        turn_status="running" if status == SessionStatus.RUNNING else "idle",
        turn_started_at=datetime.now(timezone.utc) if status == SessionStatus.RUNNING else None,
        agent_phase="executing" if status == SessionStatus.RUNNING else None,
        agent_phase_turn_no=1 if status == SessionStatus.RUNNING else None,
        agent_phase_stamped_at=datetime.now(timezone.utc) if status == SessionStatus.RUNNING else None,
        **fields,
    )


@pytest.fixture
def fake_storage_provider():
    from tests.conftest import _FakeStorageProvider

    return _FakeStorageProvider()


@pytest.fixture
def windowed(fake_storage_provider):
    inner = fake_storage_provider.get_storage(WorkspaceSession)
    sessions = _WindowedSessionStorage(inner)
    return _Provider(fake_storage_provider, sessions), sessions, inner


async def _seed(inner) -> None:
    await inner.create(_row("s-1", SessionStatus.RUNNING))
    await inner.create(_row("s-2", SessionStatus.WAITING))
    await inner.create(_row("s-3", SessionStatus.PAUSED))
    await inner.create(_row("s-done", SessionStatus.ENDED))
    await inner.create(_row("s-kept", SessionStatus.RUNNING, workspace_id="w-kept"))


@pytest.mark.asyncio
async def test_each_session_is_ended_by_one_fenced_patch_of_the_fields_it_owns(windowed) -> None:
    provider, sessions, inner = windowed
    await _seed(inner)

    reconciled = await reconcile_sessions_to_workspace_lost(provider, "w-gone")

    assert sessions.refused == [], f"whole-document writers called: {sessions.refused}"
    assert reconciled == 3
    assert sorted(i for i, _, _ in sessions.patches) == ["s-1", "s-2", "s-3"], "one write per open session of this workspace, none for the ended one or the other workspace"
    for _, patch, where in sessions.patches:
        assert set(patch) == _OWNED, "the fields the reconcile owns and nothing else: any other field would be put back from a stale snapshot"
        assert patch["status"] == "ended" and patch["ended_reason"] == "workspace_lost"
        assert patch["turn_status"] == "idle" and patch["turn_started_at"] is None
        assert patch["agent_phase"] is None and patch["agent_phase_turn_no"] is None and patch["agent_phase_stamped_at"] is None
        assert where == {"status": NON_ENDED_STATUSES()}, "an ended row must be refused by the write itself, not by the read that came before it"
    one = await inner.get("s-1")
    assert one.status == SessionStatus.ENDED and one.ended_reason == "workspace_lost" and one.ended_at is not None
    assert one.turn_status == "idle" and one.turn_started_at is None
    assert one.agent_phase is None and one.agent_phase_turn_no is None and one.agent_phase_stamped_at is None, "the phase of a turn that is gone with the workspace was left on the row"
    assert (await inner.get("s-done")).ended_reason == "completed"
    assert (await inner.get("s-kept")).status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_a_field_another_writer_committed_after_the_read_is_not_put_back(windowed) -> None:
    """A steer advanced ``last_seq`` and a cancel flag landed after the snapshot: the whole-document write restored the snapshot's values."""
    provider, sessions, inner = windowed
    await _seed(inner)

    async def another_writer() -> None:
        sessions.after_find = None
        await inner.patch_if("s-1", {"last_seq": 41, "cancel_requested": True}, where={"status": ["running"]})

    sessions.after_find = another_writer

    reconciled = await reconcile_sessions_to_workspace_lost(provider, "w-gone")

    assert sessions.refused == []
    assert reconciled == 3
    row = await inner.get("s-1")
    assert row.status == SessionStatus.ENDED and row.ended_reason == "workspace_lost"
    assert row.last_seq == 41, "the steer's last_seq was written back from the stale snapshot"
    assert row.cancel_requested is True, "the cancel flag was written back from the stale snapshot"


@pytest.mark.asyncio
async def test_a_session_ended_by_another_path_between_the_read_and_the_write_keeps_that_reason(windowed) -> None:
    """The first terminal reason wins: a turn's own end (or a preempt convergence) landed after the snapshot and must survive."""
    provider, sessions, inner = windowed
    await _seed(inner)
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    async def another_path_ends_s2() -> None:
        sessions.after_find = None
        await inner.patch_if(
            "s-2",
            {"status": "ended", "ended_reason": "cancelled", "ended_at": ended_at.isoformat(), "turn_status": "idle"},
            where={"status": NON_ENDED_STATUSES()},
        )

    sessions.after_find = another_path_ends_s2

    reconciled = await reconcile_sessions_to_workspace_lost(provider, "w-gone")

    assert sessions.refused == []
    row = await inner.get("s-2")
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "cancelled", "the reconcile overwrote the reason another path had already ended the session with"
    assert row.ended_at == ended_at, "the other path's end time was overwritten"
    assert reconciled == 2, "only the rows the reconcile actually ended are counted"
    assert (await inner.get("s-1")).ended_reason == "workspace_lost" and (await inner.get("s-3")).ended_reason == "workspace_lost"


@pytest.mark.asyncio
async def test_a_session_deleted_between_the_read_and_the_write_is_skipped_quietly(windowed, caplog) -> None:
    provider, sessions, inner = windowed
    await _seed(inner)

    async def force_delete() -> None:
        sessions.after_find = None
        await inner.delete("s-2")

    sessions.after_find = force_delete

    with caplog.at_level(logging.WARNING):
        reconciled = await reconcile_sessions_to_workspace_lost(provider, "w-gone")

    assert sessions.refused == []
    assert reconciled == 2
    assert await inner.get("s-2") is None, "a row that was deleted must not be written back by the reconcile"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], "a row that vanished is an expected race, not a failure to log with a traceback"
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "s-2" in r.getMessage()]
    assert len(warned) == 1 and "deleted" in warned[0], f"a vanished row is a warning that names the session: {warned}"


@pytest.mark.asyncio
async def test_the_fence_holds_on_a_real_sqlite_store(tmp_path) -> None:
    """The fake evaluates the guard in Python; the real backends evaluate it in the statement. Same window, real SQLite."""
    from primer.storage.sqlite import SqliteConfig, SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    try:
        inner = sp.get_storage(WorkspaceSession)
        sessions = _WindowedSessionStorage(inner)
        provider = _Provider(sp, sessions)
        await _seed(inner)
        ended_at = datetime.now(timezone.utc) - timedelta(minutes=5)

        async def another_path_ends_s2() -> None:
            sessions.after_find = None
            await inner.patch_if(
                "s-2",
                {"status": "ended", "ended_reason": "cancelled", "ended_at": ended_at.isoformat(), "turn_status": "idle"},
                where={"status": NON_ENDED_STATUSES()},
            )
            await inner.patch_if("s-1", {"last_seq": 41}, where={"status": ["running"]})

        sessions.after_find = another_path_ends_s2

        reconciled = await reconcile_sessions_to_workspace_lost(provider, "w-gone")

        assert sessions.refused == []
        assert reconciled == 2
        assert (await inner.get("s-2")).ended_reason == "cancelled"
        one = await inner.get("s-1")
        assert one.ended_reason == "workspace_lost" and one.last_seq == 41
        assert (await inner.get("s-3")).ended_reason == "workspace_lost"
        assert (await inner.get("s-done")).ended_reason == "completed"
        assert (await inner.get("s-kept")).status == SessionStatus.RUNNING
    finally:
        await sp.aclose()


def _records(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "primer.workspace.session_reconcile" and r.levelno == level]


@pytest.mark.asyncio
async def test_a_patch_refused_because_another_path_ended_the_row_is_an_info_line_that_names_the_session_and_the_reason(windowed, caplog) -> None:
    """The refusal is the fence doing its job, so it is not a warning; but it is not silent either: the line says the session was left alone and why."""
    provider, sessions, inner = windowed
    await _seed(inner)

    async def another_path_ends_s2() -> None:
        sessions.after_find = None
        await inner.patch_if("s-2", {"status": "ended", "ended_reason": "cancelled", "turn_status": "idle"}, where={"status": NON_ENDED_STATUSES()})

    sessions.after_find = another_path_ends_s2

    with caplog.at_level(logging.INFO, logger="primer.workspace.session_reconcile"):
        reconciled = await reconcile_sessions_to_workspace_lost(provider, "w-gone")

    assert reconciled == 2
    info = [m for m in _records(caplog, logging.INFO) if "s-2" in m]
    assert len(info) == 1 and "left alone" in info[0] and "ended by another path" in info[0] and "cancelled" in info[0], info
    assert not [m for m in _records(caplog, logging.WARNING) if "s-2" in m], "a row another path ended is not a warning"


@pytest.mark.asyncio
async def test_a_patch_refused_while_the_row_is_still_open_is_a_warning_that_names_the_session_and_is_not_counted(windowed, caplog) -> None:
    """A fence that refuses a row that is NOT ended (the Postgres re-check misfire of ticket 01a1247f-a1b3 was exactly this) leaves a session open on a dead workspace: that is
    never quiet. The re-read after the refusal is what tells the two apart."""
    provider, sessions, inner = windowed
    await _seed(inner)
    sessions.misrefuse = {"s-1"}

    with caplog.at_level(logging.INFO, logger="primer.workspace.session_reconcile"):
        reconciled = await reconcile_sessions_to_workspace_lost(provider, "w-gone")

    assert reconciled == 2, "a session the reconcile did not end is not counted"
    assert (await inner.get("s-1")).status == SessionStatus.RUNNING
    warned = [m for m in _records(caplog, logging.WARNING) if "s-1" in m]
    assert len(warned) == 1 and "still open" in warned[0] and "running" in warned[0], warned
    assert not [m for m in _records(caplog, logging.INFO) if "s-1" in m and "left alone" in m], "an open row was reported as left alone by another path"


@pytest.mark.asyncio
async def test_a_refusal_whose_reread_fails_is_logged_and_does_not_stop_the_others(windowed, caplog) -> None:
    provider, sessions, inner = windowed
    await _seed(inner)
    sessions.misrefuse = {"s-1"}
    sessions.get_raises = True

    with caplog.at_level(logging.INFO, logger="primer.workspace.session_reconcile"):
        reconciled = await reconcile_sessions_to_workspace_lost(provider, "w-gone")

    assert reconciled == 2, "the other sessions are still ended"
    assert (await inner.get("s-2")).ended_reason == "workspace_lost" and (await inner.get("s-3")).ended_reason == "workspace_lost"
    assert [m for m in _records(caplog, logging.WARNING) + _records(caplog, logging.ERROR) if "s-1" in m], "a refusal that cannot be explained is not silent"
