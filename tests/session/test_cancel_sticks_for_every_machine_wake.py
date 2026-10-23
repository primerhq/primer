"""An operator's Cancel sticks against every wake no person stands behind (lead ruling 1 of the G7 fixes, the audit's aside to G7-01).

Lead ruling R4 on #788 kept a session an operator cancelled from being reopened by a PEER message, and only by one: ``wake_session`` refused a
``cancelled`` target when the wake carried a ``peer``. A trigger's ``session_append`` fire, a tool call with no calling session and the restart tool
reopened it all the same. Now the refusal keys on ``human_intent``: any wake with ``human_intent=False`` (a peer message, a trigger fire, a tool call,
a queued message no person wrote, realized later) is refused, before anything is written, under the session's lifecycle lock; a person's message (the
REST steer route, a channel reply), a person's Restart (the REST restart route) and a person's steer queued before the Cancel (the lead's ruling on
ticket 01a1120e) still reopen it. A queued message keeps who wrote it: the queue marks one no person wrote and no session sent (``{"machine": True}``).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import primer.trigger.subscribers.session_append as sa
from primer.model.principal import PrincipalRef
from primer.model.trigger import SessionAppendSubConfig, Subscription, Trigger
from primer.model.user import User
from primer.model.storage import OffsetPage
from primer.model.workspace_session import MACHINE_ATTRIBUTION_KEY, PendingSessionMessage, SessionStatus
from primer.session.reset import OnlyAPersonReopens, SessionResetDeps, reset_session, restart_session
from primer.trigger.subscribers import DispatchDeps
from tests.session._peer_fakes import make_world, records, seed_session, slot

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
OWNER = PrincipalRef(type="user", id="u-admin", display="u-admin", role="admin", source="local")


async def _subscription(world) -> Subscription:
    """An admin's trigger and ``session_append`` subscription aimed at ``ses-b``: the fire out-ranks the session, so only the cancel can stop it."""
    await world.sp.get_storage(User).create(User(id="u-admin", username="u-admin", created_at=NOW, role="admin"))
    await world.sp.get_storage(Trigger).create(Trigger.model_validate({
        "id": "tr-1", "slug": "nightly", "name": "nightly", "created_at": NOW.isoformat(),
        "config": {"kind": "delayed", "fire_at": NOW.isoformat()}, "owner": OWNER.model_dump(mode="json"),
    }))
    sub = Subscription.model_validate({
        "id": "sb-1", "trigger_id": "tr-1", "config": SessionAppendSubConfig(session_id="ses-b").model_dump(mode="json"),
        "parallelism": "queue", "created_at": NOW.isoformat(), "owner": OWNER.model_dump(mode="json"),
    })
    await world.sp.get_storage(Subscription).create(sub)
    return sub


async def _fire(world, sub):
    """The real dispatcher over the real ``deliver_steer`` and ``wake_session``."""
    return await sa.SessionAppendDispatcher().dispatch(
        sub, rendered_payload="nightly report is ready", fire_context={}, fire_id="fire-1",
        deps=DispatchDeps(
            storage_provider=world.sp, claim_engine=world.engine, scheduler=world.scheduler, workspace_registry=world.registry,
            event_bus=world.bus,
        ),
    )


async def test_a_trigger_fire_does_not_reopen_a_session_an_operator_cancelled():
    world = make_world(with_bus=True)
    sub = await _subscription(world)
    target = await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, created_at=NOW)
    before = target.model_dump(mode="json")

    res = await _fire(world, sub)

    assert (res.ok, res.skipped, res.error_code) == (True, True, "skipped_session_ended"), res
    assert "cancelled" in (res.error_message or "")
    assert (await world.sessions.get("ses-b")).model_dump(mode="json") == before
    assert records(world, "ses-b") == [] and slot(world, "ses-b").reopened is False and slot(world, "ses-b").appended == []
    assert world.scheduler.enqueued == [] and world.engine.upserts == []


async def test_a_trigger_fire_still_reopens_a_session_that_ended_for_another_reason():
    world = make_world(with_bus=True)
    sub = await _subscription(world)
    await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=2, created_at=NOW)

    res = await _fire(world, sub)

    assert (res.ok, res.skipped, res.artefact_id) == (True, False, "ses-b"), res
    row = await world.sessions.get("ses-b")
    assert (row.status, row.turn_status) == (SessionStatus.RUNNING, "claimable")
    assert slot(world, "ses-b").appended == ["nightly report is ready"]


@pytest.mark.parametrize("human_intent", [False, True], ids=["no person (a tool, a peer)", "a person's Restart"])
async def test_reset_reopens_a_cancelled_session_only_for_a_person(human_intent):
    """``reset_session`` (the first half of every restart) decides it under the session's lifecycle lock, before it writes anything."""
    world = make_world(with_bus=True)
    target = await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, created_at=NOW)
    deps = SessionResetDeps(storage_provider=world.sp, workspace_registry=world.registry, event_bus=world.bus)

    if not human_intent:
        with pytest.raises(OnlyAPersonReopens, match="cancelled"):
            await reset_session(workspace_id="ws-1", session_id="ses-b", deps=deps, human_intent=False)
        assert (await world.sessions.get("ses-b")).model_dump(mode="json") == target.model_dump(mode="json")
        assert records(world, "ses-b") == [] and slot(world, "ses-b").reopened is False
        return
    row, invocation = await reset_session(workspace_id="ws-1", session_id="ses-b", deps=deps, human_intent=True)
    assert (row.status, row.ended_reason, invocation) == (SessionStatus.CREATED, None, 2)


async def test_a_restart_no_person_makes_leaves_a_cancelled_session_as_it_was():
    world = make_world(with_bus=True)
    target = await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="cancelled", turn_no=2, created_at=NOW)
    with pytest.raises(OnlyAPersonReopens):
        await restart_session(
            workspace_id="ws-1", session_id="ses-b", instruction="again", human_intent=False,
            reset_deps=SessionResetDeps(storage_provider=world.sp, workspace_registry=world.registry, event_bus=world.bus),
            wake_deps=world.wake_deps(),
        )
    assert (await world.sessions.get("ses-b")).model_dump(mode="json") == target.model_dump(mode="json")
    assert records(world, "ses-b") == [] and world.scheduler.enqueued == []


async def _pending(world) -> list[PendingSessionMessage]:
    return list((await world.sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=50))).items)


async def _cancel_and_drain(world) -> None:
    """The operator cancels the session, and its cancelled exit's drain checkpoint realizes the queue (``realize_queued_steer``)."""
    from primer.session.dispatch import realize_queued_steer

    row = await world.sessions.get("ses-b")
    await world.sessions.update(row.model_copy(update={
        "status": SessionStatus.ENDED, "ended_reason": "cancelled", "turn_status": "idle", "pause_requested": False,
    }))
    await realize_queued_steer(
        storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", scheduler=world.scheduler,
        claim_engine=world.engine, workspace_registry=world.registry, event_bus=world.bus,
    )


def _dropped(world) -> list[dict]:
    return [r["payload"] for r in records(world, "ses-b") if r["kind"] == "pause_superseded" and r["payload"]["action"] == "dropped"]


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param({"status": SessionStatus.RUNNING, "turn_status": "running"}, id="queued behind a turn"),
        pytest.param({"status": SessionStatus.PAUSED}, id="held by a pause"),
    ],
)
async def test_a_trigger_message_queued_before_the_cancel_does_not_reopen_the_session_when_it_is_realized(seed):
    world = make_world(with_bus=True)
    sub = await _subscription(world)
    await seed_session(world, sid="ses-b", turn_no=1, created_at=NOW, **seed)

    res = await _fire(world, sub)
    assert (res.ok, res.skipped) == (True, False), res
    (queued,) = await _pending(world)
    assert queued.attribution == {MACHINE_ATTRIBUTION_KEY: True}, "the queue must remember that no person wrote it"

    await _cancel_and_drain(world)

    row = await world.sessions.get("ses-b")
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "cancelled")
    assert [d["text"] for d in _dropped(world)] == ["nightly report is ready"]
    assert await _pending(world) == [] and world.scheduler.enqueued == []
    assert [r for r in records(world, "ses-b") if r["kind"] in ("user_input", "invocation_divider")] == []


async def test_a_persons_steer_held_by_a_pause_and_realized_stays_a_persons_steer():
    """The drain replays a person's queued steer as a non-human wake (``human_intent=False``: it must not clear the pause), and the pause holds it
    again. It is still a person's message: the queue stores it without the machine mark, so the cancelled exit realizes it later (ticket 01a1120e)."""
    from primer.session.dispatch import realize_queued_steer
    from primer.session.pending_messages import store_pending_steer

    world = make_world(with_bus=True)
    row = await seed_session(world, sid="ses-b", status=SessionStatus.PAUSED, turn_no=1, created_at=NOW)
    await store_pending_steer(storage_provider=world.sp, session=row, text="from a person", workspace_registry=world.registry)
    await realize_queued_steer(
        storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", scheduler=world.scheduler,
        claim_engine=world.engine, workspace_registry=world.registry, event_bus=world.bus,
    )
    (held,) = await _pending(world)
    assert held.attribution is None, "the replay through the pause hold turned a person's steer into a machine's"

    await _cancel_and_drain(world)

    row = await world.sessions.get("ses-b")
    assert (row.status, row.ended_reason, row.turn_status) == (SessionStatus.RUNNING, None, "claimable")
    assert [r["payload"]["text"] for r in records(world, "ses-b") if r["kind"] == "user_input"] == ["from a person"]
