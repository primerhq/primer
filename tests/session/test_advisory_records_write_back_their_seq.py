"""A record that takes ``last_seq + 1`` writes ``last_seq`` back (ticket 01a11cd8), so the next writer on the row does not reuse its seq.

Three writers appended a record at ``last_seq + 1`` and left the row at ``last_seq``: the claim adapter's release marker (the terminal ERROR a
failed release writes), ``wake_session``'s PAUSE_SUPERSEDED record, and the PAUSE_SUPERSEDED(dropped) record of the pending-steer cap. The next
writer seeds from the row, so it wrote its record at the same seq: two records in ``messages.jsonl`` with one seq, which every seq-keyed reader
(``after_seq`` pagination, the tap cursor, the console rows, the timeline) can lose one of.

Every test drives the production writers and then asks two things of the log: no seq repeats, and the row's ``last_seq`` is the highest seq in it.
"""

from __future__ import annotations

import json

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.model.chat import TextDelta
from primer.model.workspace_session import WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.pending_messages import _MAX_PENDING_PER_SESSION, store_pending_steer
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeExecutor,
    FakeWorkspaceIO,
    _make_lease,
    _seed_session,
    fake_event_bus,
    fake_storage_provider,
)

SID = "s-seq"


class _Slot:
    async def reopen(self) -> None: ...

    async def append_instruction(self, content, *, extra_parts=None) -> None: ...


class _Workspace(FakeWorkspaceIO):
    async def get_session(self, session_id):
        return _Slot()


class _Registry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id):
        return self._workspace

    async def get_workspace_row(self, workspace_id):
        return None


class _Scheduler:
    async def enqueue(self, session_id) -> None: ...


class _Engine:
    async def upsert(self, *args, **kwargs) -> None: ...


def _log(workspace) -> list[tuple[int, str]]:
    records = []
    for line in workspace.read_lines(SID):
        obj = json.loads(line)
        if "seq" in obj and "kind" in obj:
            records.append((obj["seq"], obj["kind"]))
    return records


async def _assert_one_seq_per_record_and_the_row_agrees(workspace, sessions) -> None:
    records = _log(workspace)
    seqs = [seq for seq, _ in records]
    assert len(seqs) == len(set(seqs)), f"two records share a seq: {records}"
    row = await sessions.get(SID)
    assert row.last_seq == max(seqs), f"the row says last_seq={row.last_seq} but the log ends at {max(seqs)}: {records}"


def _wake_deps(storage_provider, workspace, bus) -> SessionWakeDeps:
    return SessionWakeDeps(
        storage_provider=storage_provider, scheduler=_Scheduler(), claim_engine=_Engine(),
        workspace_registry=_Registry(workspace), event_bus=bus,
    )


def _isolating(sessions):
    """Make the in-memory session storage hand out COPIES on ``get`` and keep COPIES on ``create`` / ``update``, as a database does.

    The fake returns the stored object itself, so a writer that changes the row it was handed in memory (``wake_session`` bumps ``row.last_seq`` on the
    object it returns) would also change what is "stored", and an assertion on the stored row could not tell a reserved seq from an in-memory bump.
    The rows these tests read are read with ``get``; what ``create`` / ``update`` and an unwrapped ``patch_if`` RETURN is still the stored object
    itself, so a test must not assert on the identity or the mutation of a returned row.
    """
    real_get, real_create, real_update = sessions.get, sessions.create, sessions.update

    async def get(id, *, conn=None):
        row = await real_get(id)
        return None if row is None else row.model_copy(deep=True)

    async def create(entity, *, conn=None):
        return await real_create(entity.model_copy(deep=True))

    async def update(entity, *, conn=None):
        return await real_update(entity.model_copy(deep=True))

    sessions.get, sessions.create, sessions.update = get, create, update


async def _seeded(storage_provider, **fields):
    await _seed_session(storage_provider, SID)
    sessions = storage_provider.get_storage(WorkspaceSession)
    _isolating(sessions)
    row = await sessions.get(SID)
    row = await sessions.update(row.model_copy(update=fields)) if fields else row
    return sessions, row


@pytest.mark.asyncio
async def test_the_release_marker_of_a_failed_turn_writes_last_seq_back(fake_storage_provider, fake_event_bus):
    workspace = _Workspace()
    sessions, row = await _seeded(fake_storage_provider, turn_no=3, completed_turn_no=2)

    async def build(_session):
        return FakeExecutor([TextDelta(text="hi", index=0), RuntimeError("the executor blew up")])

    failed = await run_one_session_turn(
        _make_lease(SID), SessionDispatchDeps(
            storage_provider=fake_storage_provider, workspace_io=workspace, event_bus=fake_event_bus, build_executor=build,
        ),
    )
    assert failed.success is False
    adapter = SessionClaimAdapter(session_storage=sessions, workspace_registry=_Registry(workspace), event_bus=fake_event_bus)

    await adapter.on_release(None, SID, outcome=failed)

    kinds = [kind for _, kind in _log(workspace)]
    assert kinds.count("error") == 2, f"the failure exit's ERROR and the release marker: {kinds}"
    await _assert_one_seq_per_record_and_the_row_agrees(workspace, sessions)
    # and the next writer on the row (a message that reopens the failed session) takes a fresh seq
    await wake_session(
        workspace_id=row.workspace_id, session_id=SID, instruction="try again", human_intent=True,
        deps=_wake_deps(fake_storage_provider, workspace, fake_event_bus),
    )
    await _assert_one_seq_per_record_and_the_row_agrees(workspace, sessions)


@pytest.mark.asyncio
async def test_a_message_that_supersedes_a_pause_writes_last_seq_back(fake_storage_provider, fake_event_bus):
    workspace = _Workspace()
    sessions, row = await _seeded(fake_storage_provider, pause_requested=True)
    deps = _wake_deps(fake_storage_provider, workspace, fake_event_bus)

    returned = await wake_session(
        workspace_id=row.workspace_id, session_id=SID, instruction="carry on", human_intent=True, deps=deps,
    )

    assert "pause_superseded" in [kind for _, kind in _log(workspace)]
    await _assert_one_seq_per_record_and_the_row_agrees(workspace, sessions)
    stored = await sessions.get(SID)
    assert returned.last_seq == stored.last_seq == max(seq for seq, _ in _log(workspace)), (
        "wake_session returns the row it announced the pause on: its last_seq is the reserved seq, as the stored row's"
    )
    await wake_session(workspace_id=row.workspace_id, session_id=SID, instruction="and more", human_intent=True, deps=deps)
    await _assert_one_seq_per_record_and_the_row_agrees(workspace, sessions)


@pytest.mark.asyncio
async def test_an_automated_wake_queued_behind_a_pause_writes_last_seq_back(fake_storage_provider, fake_event_bus):
    workspace = _Workspace()
    sessions, row = await _seeded(fake_storage_provider, pause_requested=True)
    deps = _wake_deps(fake_storage_provider, workspace, fake_event_bus)

    returned = await wake_session(
        workspace_id=row.workspace_id, session_id=SID, instruction="from a trigger", human_intent=False, deps=deps,
    )

    assert "pause_superseded" in [kind for _, kind in _log(workspace)]
    await _assert_one_seq_per_record_and_the_row_agrees(workspace, sessions)
    assert returned.last_seq == (await sessions.get(SID)).last_seq, "the returned row carries the reserved seq too"
    await wake_session(workspace_id=row.workspace_id, session_id=SID, instruction="a human", human_intent=True, deps=deps)
    await _assert_one_seq_per_record_and_the_row_agrees(workspace, sessions)


@pytest.mark.asyncio
async def test_a_steer_dropped_by_the_pending_cap_writes_last_seq_back(fake_storage_provider, fake_event_bus):
    workspace = _Workspace()
    sessions, row = await _seeded(fake_storage_provider)

    for n in range(_MAX_PENDING_PER_SESSION + 1):
        await store_pending_steer(
            storage_provider=fake_storage_provider, session=await sessions.get(SID), text=f"steer {n}",
            workspace_registry=_Registry(workspace), event_bus=fake_event_bus,
        )

    kinds = [kind for _, kind in _log(workspace)]
    assert kinds == ["pause_superseded"], f"one drop, one announcement: {kinds}"
    await _assert_one_seq_per_record_and_the_row_agrees(workspace, sessions)
    await wake_session(
        workspace_id=row.workspace_id, session_id=SID, instruction="a human", human_intent=True,
        deps=_wake_deps(fake_storage_provider, workspace, fake_event_bus),
    )
    await _assert_one_seq_per_record_and_the_row_agrees(workspace, sessions)


@pytest.mark.asyncio
async def test_the_release_reserves_the_marker_seq_through_its_own_transaction(fake_storage_provider, fake_event_bus):
    """On Postgres a reservation on any other connection waits for the row lock the release itself holds. Only the live-Postgres test caught a
    release that dropped its ``conn`` (by its 20 s bound); this one runs in the default sweep: every storage call of the reservation carries the
    release's connection."""
    workspace = _Workspace()
    sessions, _ = await _seeded(fake_storage_provider)
    seen = []
    real_get, real_patch_if = sessions.get, sessions.patch_if

    async def get(id, *, conn=None):
        seen.append(("get", conn))
        return await real_get(id, conn=conn)

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
        seen.append(("patch_if", conn, sorted(patch or {})))
        return await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    sessions.get, sessions.patch_if = get, patch_if
    adapter = SessionClaimAdapter(session_storage=sessions, workspace_registry=_Registry(workspace), event_bus=fake_event_bus)
    transaction = object()

    from primer.int.claim import ReleaseOutcome

    await adapter.on_release(transaction, SID, outcome=ReleaseOutcome(success=False, drop_lease=True))

    reservation = [call for call in seen if call[0] == "patch_if" and call[2] == ["last_seq"]]
    assert len(reservation) == 1, seen
    assert all(call[1] is transaction for call in seen), f"a storage call left the release's transaction: {seen}"
