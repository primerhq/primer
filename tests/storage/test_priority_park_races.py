"""Priority changes and wakes share a session row. A lease is armed separately."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from primer.access.person import Person
from primer.bus.scheduler_tasks import SessionMessagingSweeper
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import ClaimKind
from primer.model.access import Access
from primer.model.api_token import ApiToken
from primer.model.except_ import AccessDeniedError, ConflictError
from primer.model.principal import PrincipalRef
from primer.model.user import User
from primer.model.workspace import Workspace
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.parked_priority import parked_priority_token, set_parked_priority
from primer.session.yields import durably_mark_session_resumable
from primer.storage.postgres import PostgresStorageProvider
from primer.workspace.session_factory import SessionFactoryDeps
from tests.pg_gate import postgres_marks
from tests.storage.test_storage_contract import provider  # noqa: F401 (SQLite and native PG)
from tests.workspace.test_session_factory import _seed_workspace


async def world(sp, *, priority_enabled=True):
    await _seed_workspace(sp)
    rows = sp.get_storage(WorkspaceSession)
    now = datetime.now(UTC)
    row = await rows.create(WorkspaceSession(
        id="target", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-1"),
        created_at=now, status=SessionStatus.RUNNING, turn_no=4,
        parked_status="parked", parked_at=now, parked_event_key="timer:target:tick",
        parked_state={"original": "preserve"}, initiated_by=PrincipalRef.system(),
    ))
    adapters = {ClaimKind.SESSION: SessionClaimAdapter(session_storage=rows)}
    if isinstance(sp, PostgresStorageProvider):
        engine = PostgresClaimEngine(storage_provider=sp, adapters=adapters, work_priority_enabled=priority_enabled)
    else:
        engine = InMemoryClaimEngine(adapters=adapters, work_priority_enabled=priority_enabled)
    return SessionFactoryDeps(sp, engine, None, nonzero_priority_available=priority_enabled), rows, row


async def edit(deps, row, priority, *, caller=None, person=Person(None, "system"), **kwargs):
    return await set_parked_priority(
        deps, workspace_id=row.workspace_id, session_id=row.id, priority=priority,
        generation=parked_priority_token(row), person=person, caller_id=caller,
        caller_turn=4 if caller else None, **kwargs,
    )


async def wake(rows, row, engine):
    return await durably_mark_session_resumable(
        row, event_key=row.parked_event_key, payload={"answer": "ok"}, session_storage=rows, engine=engine,
    )


async def priority_claim(engine, priority):
    if priority:
        assert await engine.claim_due("foreground", max_count=1, max_work_priority=priority - 1) == []
    lease, = await engine.claim_due("worker", max_count=1)
    assert (lease.entity_id, lease.work_priority) == ("target", priority)


@pytest.mark.parametrize("repair", ["redelivery", "sweeper"])
@pytest.mark.parametrize("boot_arm", [False, True])
async def test_wake_recovery_uses_the_edited_priority(provider, repair, boot_arm):
    deps, rows, before = await world(provider)
    if boot_arm:
        await deps.claim_engine.mark_resumable(ClaimKind.SESSION, before.id)
    after = await edit(deps, before, 15)
    assert after.parked_state == before.parked_state and after.turn_no == before.turn_no
    assert await deps.claim_engine.has_lease(ClaimKind.SESSION, before.id) is boot_arm
    # Stop at the same durable boundary as a crash after the flip and before arm.
    assert await wake(rows, before, None)
    assert await deps.claim_engine.has_lease(ClaimKind.SESSION, before.id) is boot_arm
    assert await deps.claim_engine.claim_due("before-repair", max_count=1) == []
    with pytest.raises(ConflictError):
        await edit(deps, after, 1)
    if repair == "redelivery":
        assert await wake(rows, before, deps.claim_engine)
    else:
        sweeper = SessionMessagingSweeper(
            storage_provider=provider, claim_engine=deps.claim_engine, scheduler=None, workspace_registry=None,
        )
        assert await sweeper._repair_resumable_parks() == 1
        assert await sweeper._repair_resumable_parks() == 0
    await priority_claim(deps.claim_engine, 15)
    assert (await rows.get(before.id)).turn_no == before.turn_no


async def test_lease_matching_before_cutover_only_checks_existence(provider):
    deps, _, parked = await world(provider, priority_enabled=False)
    engine = deps.claim_engine
    assert not await engine.has_lease(ClaimKind.SESSION, parked.id, work_priority=17)
    await engine.mark_resumable(ClaimKind.SESSION, parked.id)
    assert await engine.has_lease(ClaimKind.SESSION, parked.id, work_priority=0)
    assert await engine.has_lease(ClaimKind.SESSION, parked.id, work_priority=17)


async def test_parked_edit_preserves_queued_child_session_priority(provider):
    deps, rows, original = await world(provider)
    parent = await edit(deps, original, 18)
    child = await rows.create(WorkspaceSession(
        id="child", parent_session_id=parent.id, workspace_id=parent.workspace_id,
        binding=parent.binding, created_at=parent.created_at, status=SessionStatus.RUNNING,
        priority=parent.priority, initiated_by=parent.initiated_by,
    ))
    await deps.claim_engine.mark_resumable(ClaimKind.SESSION, child.id)
    assert (await edit(deps, parent, 2)).priority == 2
    assert (await rows.get(child.id)).priority == 18
    assert await deps.claim_engine.claim_due("foreground", max_count=1, max_work_priority=2) == []
    lease, = await deps.claim_engine.claim_due("background", max_count=1, max_work_priority=18)
    assert (lease.entity_id, lease.work_priority) == (child.id, 18)


async def test_competing_edits_and_return_to_original_value(provider):
    deps, rows, before = await world(provider)
    outcomes = await asyncio.gather(edit(deps, before, 5), edit(deps, before, 40), return_exceptions=True)
    changed, = [item for item in outcomes if isinstance(item, WorkspaceSession)]
    assert sum(isinstance(item, ConflictError) for item in outcomes) == 1
    assert changed.priority in (5, 40)
    restored = await edit(deps, changed, 0)
    assert restored.priority_revision == 2
    with pytest.raises(ConflictError):
        await edit(deps, before, 7)
    assert (await rows.get(before.id)).priority == 0


@pytest.mark.parametrize("change", ["binding", "repark", "cancel", "end", "recreate"])
async def test_old_park_cannot_update_changed_lifecycle(provider, change):
    deps, rows, before = await world(provider)
    patches = {
        "binding": {"binding_epoch": before.binding_epoch + 1},
        "repark": {"parked_at": (before.parked_at + timedelta(seconds=1)).isoformat()},
        "cancel": {"cancel_requested": True}, "end": {"status": "ended"},
    }
    if change == "recreate":
        await rows.delete(before.id)
        await rows.create(before.model_copy(update={"created_at": before.created_at + timedelta(seconds=1)}))
    else:
        assert await rows.patch_if(before.id, patches[change], where={"turn_no": [4]})
    with pytest.raises(ConflictError):
        await edit(deps, before, 12)
    assert (await rows.get(before.id)).priority_revision == 0


@pytest.mark.parametrize("loss", ["target", "caller_workspace", "account", "token", "caller_cancel", "caller_priority"])
async def test_authority_lost_between_caller_read_and_lock(provider, monkeypatch, loss):
    deps, rows, before = await world(provider)
    await _seed_workspace(provider, "other")
    workspaces = provider.get_storage(Workspace)
    for wid in ("ws-1", "other"):
        assert await workspaces.patch_if(wid, {"access": Access(owners=["u"]).model_dump(mode="json")}, where={"phase": ["pending"]})
    user = User(id="u", username="driver", created_at=before.created_at)
    await provider.get_storage(User).create(user)
    await provider.get_storage(ApiToken).create(ApiToken(
        id="token", name="driver", prefix="12345678", token_hash="0"*64, user_id="u", created_at=before.created_at,
    ))
    actor = PrincipalRef(type="api_token", id="token", display="driver", user_id="u", role="user", source="internal")
    await rows.create(before.model_copy(update={"id": "caller", "workspace_id": "other", "parked_status": None,
                                               "parked_at": None, "initiated_by": actor, "priority": 10}))
    person = Person("u", "user")
    with pytest.raises(AccessDeniedError):
        await edit(deps, before, 0, caller="caller", person=person)
    original = provider.locking_transaction

    @asynccontextmanager
    async def after_read(*, models):
        if loss in ("target", "caller_workspace"):
            wid = "ws-1" if loss == "target" else "other"
            assert await workspaces.patch_if(wid, {"access": Access(public=True).model_dump(mode="json")}, where={"phase": ["pending"]})
        elif loss == "account":
            assert await provider.get_storage(User).patch_if("u", {"disabled": True}, where={"role": ["user"]})
        elif loss == "token":
            assert await provider.get_storage(ApiToken).patch_if("token", {"revoked_at": before.created_at.isoformat()}, where={"user_id": ["u"]})
        else:
            patch = {"priority": 5} if loss == "caller_priority" else {"cancel_requested": True}
            assert await rows.patch_if("caller", patch, where={"turn_no": [4]})
        async with original(models=models) as conn:
            yield conn

    monkeypatch.setattr(provider, "locking_transaction", after_read)
    expected = ConflictError if loss.startswith("caller_") and loss != "caller_workspace" else AccessDeniedError
    with pytest.raises(expected):
        await edit(deps, before, 12, caller="caller", person=person)
    assert (await rows.get(before.id)).priority == 0


@pytest.mark.parametrize("credential", ["token", "cookie"])
async def test_direct_edit_rechecks_token_expiry_or_cookie_epoch(provider, credential):
    deps, rows, parked = await world(provider)
    users = provider.get_storage(User)
    user = await users.create(User(id="driver", username="driver", role="admin", created_at=parked.created_at))
    person = Person(user.id, user.role)
    if credential == "token":
        tokens = provider.get_storage(ApiToken)
        token = await tokens.create(ApiToken(
            id="token", name="driver", prefix="12345678", token_hash="0" * 64, user_id=user.id,
            created_at=parked.created_at, expires_at=parked.created_at + timedelta(hours=1),
        ))
        witness = {"token_id": token.id}
    else:
        witness = {"account_epoch": user.session_epoch}

    # A current witness can edit this park. Keep the resulting generation
    # current while only the caller's authority changes.
    current = await edit(deps, parked, 4, person=person, **witness)
    if credential == "token":
        expired = (parked.created_at - timedelta(seconds=1)).isoformat()
        assert await tokens.patch_if(token.id, {"expires_at": expired}, where={"user_id": [user.id]})
    else:
        assert await users.patch_if(user.id, {"session_epoch": user.session_epoch + 1}, where={"role": [user.role]})
    with pytest.raises(AccessDeniedError):
        await edit(deps, current, 7, person=person, **witness)
    saved = await rows.get(parked.id)
    assert (saved.priority, saved.priority_revision) == (4, current.priority_revision)


@pytest.mark.parametrize("edit_first", [True, False])
@pytest.mark.parametrize("provider", [pytest.param("postgres", marks=postgres_marks())], indirect=True)
async def test_native_waiting_wake_and_edit_serialize_at_the_park(provider, edit_first):
    deps, rows, before = await world(provider)
    # Prepare all lazy tables before the independent blocker takes the row.
    await edit(deps, before, 0)
    actions = [lambda: edit(deps, before, 27), lambda: wake(rows, before, deps.claim_engine)]
    tasks = []
    try:
        async with asyncio.timeout(20):
            async with provider.pool.acquire() as blocker, provider.pool.acquire() as observer:
                async with blocker.transaction():
                    await blocker.fetchval(f'SELECT id FROM "{provider.schema}".sessions WHERE id=$1 FOR UPDATE', before.id)
                    waiting = set()
                    for action in actions if edit_first else reversed(actions):
                        tasks.append(asyncio.create_task(action()))
                        # Wait for a real PG lock wait, including the second
                        # transaction queued behind the first waiter.
                        while True:
                            assert not tasks[-1].done(), tasks[-1].exception()
                            blocked = await observer.fetch(
                                "SELECT pid, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
                                "WHERE datname=current_database() AND wait_event_type='Lock'"
                            )
                            found = {r["pid"] for r in blocked if set(r["blockers"]) & (waiting | {blocker.get_server_pid()})}
                            if found - waiting:
                                waiting |= found
                                break
                            await asyncio.sleep(.01)
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            if edit_first:
                assert isinstance(outcomes[0], WorkspaceSession) and outcomes[0].priority == 27
                assert outcomes[1] is True
            else:
                assert outcomes[0] is True and isinstance(outcomes[1], ConflictError)
            final = await rows.get(before.id)
            assert final.parked_status == "resumable" and final.parked_state["original"] == "preserve"
            await priority_claim(deps.claim_engine, 27 if edit_first else 0)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_old_raw_park_spelling_and_missing_revision(provider):
    deps, rows, before = await world(provider)
    spelling = before.parked_at.isoformat()
    if isinstance(provider, PostgresStorageProvider):
        async with provider.pool.acquire() as conn:
            await conn.execute(
                f"""UPDATE "{provider.schema}".sessions
                    SET data=jsonb_set(data - 'priority_revision', '{{parked_at}}', to_jsonb($1::text))
                    WHERE id=$2""", spelling, before.id,
            )
    else:
        await provider.connection.execute(
            "UPDATE sessions SET data=json_set(json_remove(data, '$.priority_revision'), '$.parked_at', ?) WHERE id=?",
            (spelling, before.id),
        )
        await provider.connection.commit()
    old = await rows.get(before.id)
    assert old.priority_revision == 0
    edited = await edit(deps, old, 11)
    assert (edited.priority, edited.priority_revision) == (11, 1)


@pytest.mark.parametrize("edit_first", [True, False])
async def test_in_memory_wake_and_edit_across_await(fake_storage_provider, monkeypatch, edit_first):
    deps, rows, before = await world(fake_storage_provider)
    entered, release = asyncio.Event(), asyncio.Event()
    # Suspend the later writer after it has the old park but before it can
    # take the writer lock. The other operation runs to completion.
    if edit_first:
        original = rows.patch_if

        async def hold_wake(*args, **kwargs):
            if kwargs.get("conn") is None:
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(rows, "patch_if", hold_wake)
        delayed = wake(rows, before, deps.claim_engine)
    else:
        original = fake_storage_provider.locking_transaction

        @asynccontextmanager
        async def hold_edit(*, models):
            entered.set()
            await release.wait()
            async with original(models=models) as conn:
                yield conn

        monkeypatch.setattr(fake_storage_provider, "locking_transaction", hold_edit)
        delayed = edit(deps, before, 20)
    task = asyncio.create_task(delayed)
    try:
        async with asyncio.timeout(6):
            await entered.wait()
            if edit_first:
                assert (await edit(deps, before, 20)).priority == 20
                release.set()
                assert await task
            else:
                assert await wake(rows, before, deps.claim_engine)
                release.set()
                with pytest.raises(ConflictError):
                    await task
            await priority_claim(deps.claim_engine, 20 if edit_first else 0)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
