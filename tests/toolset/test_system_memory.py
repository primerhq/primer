"""The owner tools for an agent's memory (agent memory T13): the tool twins of the entry routes, with the same owner rule."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from primer.events.recorder import recorder_for
from primer.knowledge.document_service import DocumentService
from primer.memory.collection import memory_collection_id
from primer.memory.store import MemoryStore
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection
from primer.model.memory import MemoryAdd
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset._system_memory import memory_tools
from primer.toolset.system import build_system_toolset

AGENT = "agent-m"
NAMES = ("list_agent_memory", "approve_agent_memory_entry", "archive_agent_memory_entry", "edit_agent_memory_entry")


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    await provider.get_event_store().ensure_schema()
    for uid in ("alice", "bob", "carol", "dave"):
        await provider.get_storage(User).create(User(id=uid, username=uid, password_hash=None, created_at=datetime.now(UTC), role="user"))
    await provider.get_storage(Agent).create(Agent(
        id=AGENT, description="a", model=AgentModel(profile_id="p"), access=Access(owners=["alice"], grants={"bob": "read", "carol": "full"}),
    ))
    await provider.get_storage(Collection).create(Collection(id=memory_collection_id(AGENT), description="memory", memory_of=AGENT))
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.fixture
def tools(sp) -> dict:
    return memory_tools(storage_provider=sp)


@pytest.fixture
def store(sp) -> MemoryStore:
    return MemoryStore(sp, DocumentService(sp), recorder_for(sp))


async def _seed(store: MemoryStore, name: str = "Retry GitHub 429", *, needs_approval: bool = False) -> str:
    add = MemoryAdd(op="add", name=name, description="d", steps=["wait"], pitfalls=[], example=None, topics=[])
    entry = await store.add_candidate(AGENT, add, source={"session_id": "s", "from_turn": 1, "to_turn": 2}, taught_by="alice", needs_approval=needs_approval)
    return entry.name


def _ctx(uid: str) -> ToolContext:
    ref = PrincipalRef(type="user", id=uid, display=uid, role="user", source="local", user_id=uid)
    return ToolContext(tool_call_id="c1", session_id="s1", workspace_id="w1", initiated_by=ref)


async def _call(tools: dict, name: str, args: dict, uid: str):
    _tool, handler = tools[name]
    result = await handler(args, ctx=_ctx(uid))
    return result.is_error, json.loads(result.output)


async def test_tools_listed_and_callable_over_mcp(sp, tools, store) -> None:
    system = build_system_toolset(storage_provider=sp, provider_registry=object())
    listed = {t.id: t async for t in system.list_tools()}
    for name in NAMES:
        assert name in listed and system.required_role(name) == "user", name
        assert tools[name][0].toolset_id == "system"
    name = await _seed(store)
    is_err, out = await _call(tools, "list_agent_memory", {"agent_id": AGENT}, "bob")
    assert not is_err and [(e["name"], e["status"]) for e in out["items"]] == [(name, "candidates")]
    assert out["items"][0]["body"]["steps"] == [{"id": "s1", "text": "wait"}]
    is_err, out = await _call(tools, "list_agent_memory", {"agent_id": AGENT, "status": "active"}, "bob")
    assert not is_err and out["items"] == []


async def test_non_owner_refused(sp, tools, store) -> None:
    name = await _seed(store)
    for uid in ("bob", "carol"):
        for tool, args in (
            ("approve_agent_memory_entry", {"agent_id": AGENT, "name": name}),
            ("archive_agent_memory_entry", {"agent_id": AGENT, "name": name}),
            ("edit_agent_memory_entry", {"agent_id": AGENT, "name": name, "description": "x"}),
        ):
            is_err, out = await _call(tools, tool, args, uid)
            assert is_err and out["type"] == "forbidden", (uid, tool, out)
    is_err, out = await _call(tools, "list_agent_memory", {"agent_id": AGENT}, "dave")
    assert is_err and out["type"] == "not-found", "an agent a person cannot read does not exist to them"
    is_err, out = await _call(tools, "approve_agent_memory_entry", {"agent_id": AGENT, "name": name}, "dave")
    assert is_err and out["type"] == "not-found"
    entry = await store.get(AGENT, name)
    assert entry.status == "candidates" and entry.description == "d"


async def test_approve_and_archive_move_entries(sp, tools, store) -> None:
    one, two = await _seed(store, "One", needs_approval=True), await _seed(store, "Two")
    is_err, out = await _call(tools, "approve_agent_memory_entry", {"agent_id": AGENT, "name": one}, "alice")
    assert not is_err and out["status"] == "active" and out["meta"]["needs_approval"] is False
    is_err, out = await _call(tools, "archive_agent_memory_entry", {"agent_id": AGENT, "name": two}, "alice")
    assert not is_err and out["status"] == "archived"
    is_err, out = await _call(tools, "edit_agent_memory_entry", {"agent_id": AGENT, "name": one, "description": "better", "pinned": True}, "alice")
    assert not is_err and out["description"] == "better" and out["meta"]["pinned"] is True
    is_err, out = await _call(tools, "approve_agent_memory_entry", {"agent_id": AGENT, "name": "nope"}, "alice")
    assert is_err and out["type"] == "not-found"
    is_err, out = await _call(tools, "approve_agent_memory_entry", {"agent_id": AGENT, "name": two}, "alice")
    assert is_err and out["type"] == "conflict", "an archived entry cannot be approved"
    events = await sp.get_event_store().read_after(0, limit=50, event_type_prefix="memory.entry.")
    assert {e.actor for e in events if e.event_type in ("memory.entry.approved", "memory.entry.archived")} == {"user:alice"}
