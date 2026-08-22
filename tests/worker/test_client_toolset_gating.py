"""Client-toolset gating: attachment decides ONCE per turn (S3 s4, M2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from primer.model.client_attachment import ClientAttachment
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.agent import Agent, AgentModel
from primer.model.yield_ import Yielded
from primer.session.attachment import ATTACH_TTL_SECONDS, detach
from primer.toolset.client import CLIENT_TOOLSET_ID
from primer.worker.executor_builders import client_tools_attached, client_toolset_for
from primer.worker.yield_runtime import ParkedState
from tests.conftest import _FakeStorageProvider


class _Pool:
    def __init__(self, storage) -> None:
        self._storage = storage


def _session(**over) -> WorkspaceSession:
    kwargs = dict(
        id="s-1",
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.CREATED,
        created_at=datetime.now(UTC),
    )
    kwargs.update(over)
    return WorkspaceSession(**kwargs)


async def _attach(storage_provider, *, expires_in: float) -> None:
    now = datetime.now(UTC)
    await storage_provider.get_storage(ClientAttachment).create(
        ClientAttachment(
            workspace_id="ws-1",
            session_id="s-1",
            client_id="tab-a",
            attached_seq=0,
            expires_at=now + timedelta(seconds=expires_in),
            created_at=now,
        )
    )


async def test_no_attachment_means_no_client_toolset() -> None:
    pool = _Pool(_FakeStorageProvider())
    assert await client_tools_attached(pool, _session()) is False
    assert await client_toolset_for(pool, _session()) == {}


async def test_live_attachment_registers_the_client_toolset() -> None:
    sp = _FakeStorageProvider()
    await _attach(sp, expires_in=ATTACH_TTL_SECONDS)
    pool = _Pool(sp)
    assert await client_tools_attached(pool, _session()) is True
    providers = await client_toolset_for(pool, _session())
    assert list(providers) == [CLIENT_TOOLSET_ID]


async def test_expired_attachment_does_not_count() -> None:
    sp = _FakeStorageProvider()
    await _attach(sp, expires_in=-1.0)
    pool = _Pool(sp)
    assert await client_tools_attached(pool, _session()) is False


async def test_client_tools_ignore_allow_external_tools() -> None:
    """Spec section 3's gating split, from the client-tools side.

    ``allow_external_tools`` gates ONLY API-caller externals; notifying
    client tools are gated by attachment alone. So the helper never reads
    an agent at all, and a session bound to an agent that FORBIDS external
    tools still carries the client toolset while a browser is attached.
    """
    import inspect

    assert list(inspect.signature(client_toolset_for).parameters) == [
        "pool",
        "session_row",
    ], "client_toolset_for must not grow an agent parameter"

    sp = _FakeStorageProvider()
    await _attach(sp, expires_in=ATTACH_TTL_SECONDS)
    forbids_externals = _session(
        binding=AgentSessionBinding(
            agent_id="ag1",
            agent_snapshot=Agent(
                id="ag1",
                description="x",
                model=AgentModel(profile_id="p--m"),
                allow_external_tools=False,
            ),
        ),
        external_tools=None,
    )
    providers = await client_toolset_for(_Pool(sp), forbids_externals)
    assert list(providers) == [CLIENT_TOOLSET_ID]


async def test_detach_mid_turn_does_not_revoke_the_running_turn() -> None:
    """Spec section 7: detach mid-turn only stops DELIVERY.

    The decision is made ONCE, at build time. A browser closing afterwards
    cannot un-build the toolset the turn is already carrying; what it
    changes is what the NEXT build reads.
    """
    sp = _FakeStorageProvider()
    await _attach(sp, expires_in=ATTACH_TTL_SECONDS)
    pool = _Pool(sp)
    providers = await client_toolset_for(pool, _session())
    assert list(providers) == [CLIENT_TOOLSET_ID]

    removed = await detach(
        sp.get_storage(ClientAttachment), session_id="s-1", client_id="tab-a"
    )
    assert removed is True
    # The built toolset is untouched: nothing re-reads attachment mid-turn.
    assert list(providers) == [CLIENT_TOOLSET_ID]
    # The next turn's build sees the detach.
    assert await client_tools_attached(pool, _session()) is False


async def test_resume_reuses_the_frozen_flag_not_live_attachment() -> None:
    # No live attachment at all, yet the parked turn keeps its toolset.
    pool = _Pool(_FakeStorageProvider())
    parked = _session(
        parked_status="parked",
        parked_state={"client_tools_attached": True},
    )
    assert await client_tools_attached(pool, parked) is True

    # And the inverse: a turn that started detached does not gain the
    # toolset just because a browser attached while it was parked.
    sp = _FakeStorageProvider()
    await _attach(sp, expires_in=ATTACH_TTL_SECONDS)
    parked_off = _session(
        parked_status="parked",
        parked_state={"client_tools_attached": False},
    )
    assert await client_tools_attached(_Pool(sp), parked_off) is False


def test_parked_state_round_trips_the_flag() -> None:
    ps = ParkedState(
        yielded=Yielded(tool_name="ask_user", event_key="k", resume_metadata={}),
        llm_messages=[],
        turn_no=1,
        started_at=datetime.now(UTC),
        client_tools_attached=True,
    )
    blob = ps.to_jsonable()
    assert blob["client_tools_attached"] is True
    assert ParkedState.from_jsonable(blob).client_tools_attached is True


def test_parked_state_defaults_the_flag_for_old_blobs() -> None:
    ps = ParkedState(
        yielded=Yielded(tool_name="ask_user", event_key="k", resume_metadata={}),
        llm_messages=[],
        turn_no=1,
        started_at=datetime.now(UTC),
    )
    blob = ps.to_jsonable()
    blob.pop("client_tools_attached")
    assert ParkedState.from_jsonable(blob).client_tools_attached is False
