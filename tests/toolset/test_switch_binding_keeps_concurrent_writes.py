"""``switch_binding`` (and ``switch_to_agent``) keep what lands on the session row while they queue a switch (AG-M13).

The tool read the session row mid-turn and wrote it whole: a Stop, a Cancel or a seq a steer reserved between the read and the
write was erased. As the REST twin, it now writes ``pending_binding_switch`` alone under the lifecycle lock, fenced on the
``turn_status`` and ``last_seq`` read inside it; a refused fence answers ``type=conflict`` and writes nothing.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from primer.api.registries import ProviderRegistry
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.common import dump_for_storage
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext
from primer.session.seq_reservation import reserve_next_seq
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.system import build_system_toolset


def _race_after_read(monkeypatch, storage, hook) -> None:
    """``storage.get`` runs ``hook`` once, right after the first read: a concurrent writer landing in the read-write window."""
    real = storage.get
    pending = [hook]

    async def get(id, **kw):  # noqa: A002
        row = await real(id, **kw)
        if pending:
            await pending.pop()()
        return row

    monkeypatch.setattr(storage, "get", get)


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "s.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


# --- switch_binding (AG-M13) --------------------------------------------------------------------------------------------


async def _session_world(sp):
    for aid in ("agent-a", "agent-b"):
        await sp.get_storage(Agent).create(Agent(access=Access(public=True), id=aid, description=aid,
                                                 model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[]))
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="agent-a"),
                                           status=SessionStatus.RUNNING, created_at=datetime.now(UTC), turn_status="running",
                                           last_seq=10))
    registry = ProviderRegistry(sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
                                cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object())
    toolset = build_system_toolset(storage_provider=sp, provider_registry=registry)
    who = PrincipalRef(type="user", id="u1", display="u", role="admin", source="local")
    ctx = ToolContext(tool_call_id="tc", session_id="s1", workspace_id="w1", initiated_by=who)
    return sessions, toolset, ctx


QUEUED = {"kind": "agent", "agent_id": "agent-b", "graph_id": None, "profile_id": None, "actor": "agent", "reason": "r"}


@pytest.mark.parametrize("tool", ["switch_binding", "switch_to_agent"])
async def test_switch_queues_the_request_and_nothing_else(sp, tool) -> None:
    sessions, toolset, ctx = await _session_world(sp)
    before = dump_for_storage(await sessions.get("s1"))
    _, handler = toolset._registry[tool]  # noqa: SLF001
    args = {"kind": "agent", "agent_id": "agent-b", "reason": "r"} if tool == "switch_binding" else {"agent_id": "agent-b", "prompt": "r"}
    res = await handler(args, ctx=ctx)
    assert (res.is_error, res.output) == (False, "binding switch to 'agent-b' queued; applies at the end of this turn")
    after = dump_for_storage(await sessions.get("s1"))
    assert after.pop("pending_binding_switch") == QUEUED
    before.pop("pending_binding_switch")
    assert after == before


async def test_switch_on_a_missing_session_is_not_found(sp) -> None:
    _, toolset, ctx = await _session_world(sp)
    _, handler = toolset._registry["switch_binding"]  # noqa: SLF001
    res = await handler({"kind": "agent", "agent_id": "agent-b"}, ctx=dataclasses.replace(ctx, session_id="nope"))
    assert res.is_error and json.loads(res.output) == {"type": "not-found", "message": "session 'nope' does not exist"}


async def test_switch_keeps_a_concurrent_cancel_and_reserved_seq(sp, monkeypatch) -> None:
    sessions, toolset, ctx = await _session_world(sp)

    async def concurrent_writers():
        # A REST Stop/Cancel lands on the row, and a steer reserves the next seq.
        await sessions.patch_if("s1", {"cancel_requested": True, "interrupt_requested": True}, where={"status": ["running"]})
        assert await reserve_next_seq(sessions, "s1") == 11

    _race_after_read(monkeypatch, sessions, concurrent_writers)
    _, handler = toolset._registry["switch_binding"]  # noqa: SLF001
    res = await handler({"kind": "agent", "agent_id": "agent-b"}, ctx=ctx)
    monkeypatch.undo()
    after = await sessions.get("s1")
    assert (after.cancel_requested, after.interrupt_requested, after.last_seq) == (True, True, 11)
    assert res.is_error and json.loads(res.output)["type"] == "conflict"
    assert after.pending_binding_switch is None
