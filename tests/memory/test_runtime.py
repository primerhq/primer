"""Agent memory at run time (agent memory Task 10): the tools, the index and the instructions a memory-enabled agent's session runs with."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.events.recorder import recorder_for
from primer.knowledge.document_service import DocumentService
from primer.llm._tokenizer.char_fallback import text_tokens
from primer.memory.collection import memory_collection_id
from primer.memory.entry import EntryBody
from primer.memory.runtime import (
    MEMORY_TOOL_IDS,
    build_index,
    memory_instructions,
    memory_prompt_parts,
    with_memory_tools,
)
from primer.memory.store import MemoryEntry, MemoryStore
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection
from primer.model.memory import AgentMemory, MemoryAdd
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider

pytestmark = pytest.mark.asyncio

AGENT = "a1"
CID = memory_collection_id(AGENT)
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _entry(name: str, status: str = "active", description: str = "when it applies") -> MemoryEntry:
    return MemoryEntry(name=name, status=status, description=description, body=EntryBody(steps=[("s1", "do")], pitfalls=[], example=[]))


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    await provider.get_event_store().ensure_schema()
    await provider.get_storage(Collection).create(Collection(id=CID, description="memory", memory_of=AGENT))
    yield provider
    await provider.aclose()


def _agent(system_prompt: list[str] | None = None, tools: list[str] | None = None) -> Agent:
    return Agent(id=AGENT, description="d", model=AgentModel(profile_id="p--m"), system_prompt=system_prompt or ["You help."], tools=tools or [])


async def _session(sp) -> WorkspaceSession:
    return await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id=AGENT), status=SessionStatus.RUNNING, created_at=NOW,
    ))


async def _reread(sp) -> WorkspaceSession:
    return await sp.get_storage(WorkspaceSession).get("s1")


async def _enable(sp, **fields) -> None:
    await sp.get_storage(AgentMemory).create(AgentMemory(id=AGENT, enabled=True, **fields))


async def _active(sp, name: str, description: str = "when it applies") -> None:
    store = MemoryStore(sp, DocumentService(sp), recorder_for(sp))
    src = {"session_id": "s0", "from_turn": 1, "to_turn": 2}
    entry = await store.add_candidate(
        AGENT, MemoryAdd(op="add", name=name, description=description, steps=["do"], pitfalls=[], topics=[]), source=src, taught_by="owner",
        needs_approval=False,
    )
    await store.approve(AGENT, entry.name, actor="user:o")


def test_tools_always_include_search_and_read_document_when_memory_on():
    assert MEMORY_TOOL_IDS == ("collections__search", "collections__read_document")
    assert with_memory_tools(["system__get_agent"]) == ["system__get_agent", "collections__search", "collections__read_document"]
    assert with_memory_tools(["collections__read_document", "x"]) == ["collections__read_document", "x", "collections__search"]
    both = ["collections__search", "collections__read_document"]
    assert with_memory_tools(both) == both
    assert with_memory_tools([]) == both


async def test_tools_unchanged_when_memory_off(sp):
    await _session(sp)
    assert await memory_prompt_parts(sp, _agent(), await _reread(sp)) == ([], "")
    await sp.get_storage(AgentMemory).create(AgentMemory(id=AGENT, enabled=False))
    assert await memory_prompt_parts(sp, _agent(), await _reread(sp)) == ([], "")


def test_index_lists_active_only_and_respects_both_caps():
    entries = [_entry("alpha"), _entry("beta"), _entry("gamma"), _entry("cand", "candidates"), _entry("old", "archived")]
    block = build_index(entries, max_entries=2, context_length=1_000_000)
    assert block.splitlines() == ["- alpha: when it applies", "- beta: when it applies"], "active only, capped by max_entries"
    many = [_entry(f"entry-{i:03d}", description="a line that costs some tokens to show") for i in range(200)]
    small = build_index(many, max_entries=200, context_length=4_000)
    assert 0 < len(small.splitlines()) < 200
    assert text_tokens(small) <= 4_000 * 0.02, "the block stays at or under 2% of the context"
    assert build_index([], max_entries=5, context_length=8_000) == ""


async def test_the_instructions_name_the_collection_and_the_two_tools():
    text = memory_instructions(CID)
    assert CID in text and "search" in text and 'path_prefix' in text and "active/" in text and "read_document" in text


async def test_parts_carry_the_instructions_and_the_index_block(sp):
    await _enable(sp)
    await _active(sp, "retry-429", "when a call is rate limited")
    await _session(sp)
    parts, index = await memory_prompt_parts(sp, _agent(), await _reread(sp))
    assert index == "- retry-429: when a call is rate limited"
    assert any(index in p for p in parts) and any(memory_instructions(CID) == p for p in parts)


async def test_index_frozen_for_the_session(sp):
    await _enable(sp)
    await _active(sp, "first")
    await _session(sp)
    _parts, first = await memory_prompt_parts(sp, _agent(), await _reread(sp))
    await _active(sp, "second")
    second_parts, second = await memory_prompt_parts(sp, _agent(), await _reread(sp))
    assert "first" in first and "second" not in first
    assert second == first and "second" not in "".join(second_parts), "an entry added after the first turn is absent from the second turn's prompt"
    assert (await _reread(sp)).metadata["_memory_index"] == first, "stored in the session's metadata under the system key"
    # a new session of the same agent sees it
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s2", workspace_id="w1", binding=AgentSessionBinding(agent_id=AGENT), status=SessionStatus.RUNNING, created_at=NOW,
    ))
    _p, fresh = await memory_prompt_parts(sp, _agent(), await sp.get_storage(WorkspaceSession).get("s2"))
    assert "second" in fresh


async def test_ctx_memory_index_placement_omits_automatic_block_keeps_instructions(sp):
    await _enable(sp)
    await _active(sp, "retry-429")
    await _session(sp)
    mine = _agent(["Notes: {{ ctx.memory_index }}"])
    parts, index = await memory_prompt_parts(sp, mine, await _reread(sp))
    assert index == "- retry-429: when it applies"
    assert parts == [memory_instructions(CID)], "no automatic index block, the instructions are always appended"


async def test_an_empty_memory_still_gets_the_instructions(sp):
    await _enable(sp)
    await _session(sp)
    parts, index = await memory_prompt_parts(sp, _agent(), await _reread(sp))
    assert index == "" and parts == [memory_instructions(CID)]


def _executor(agent: Agent, **kwargs) -> WorkspaceAgentExecutor:
    session = SimpleNamespace(system_prompt_fragment="WS FRAGMENT", workspace_id="w1", session_id="s1")
    return WorkspaceAgentExecutor(
        agent=agent, llm=SimpleNamespace(), llm_model=SimpleNamespace(context_length=8000), tool_manager=SimpleNamespace(), session=session, **kwargs,
    )


def test_the_executor_appends_the_memory_parts_after_the_workspace_parts_and_exposes_the_index():
    ex = _executor(_agent(["You help."]), memory_parts=["INSTRUCTIONS", "INDEX"], memory_index="- a: b")
    assert ex._agent.system_prompt == ["You help.", "WS FRAGMENT", "INSTRUCTIONS", "INDEX"]
    assert ex._execution_context.memory_index == "- a: b"


def test_graph_node_and_invoke_paths_unchanged():
    ex = _executor(_agent(["You help."]))
    assert ex._agent.system_prompt == ["You help.", "WS FRAGMENT"] and ex._execution_context.memory_index == ""
    root = Path(__file__).resolve().parents[2] / "primer"
    for path in [*(root / "graph").rglob("*.py"), root / "agent" / "invoke.py"]:
        assert "primer.memory" not in path.read_text(encoding="utf-8"), f"{path} must not use agent memory"
    builders = (root / "worker" / "executor_builders.py").read_text(encoding="utf-8")
    graph_part = builders[builders.index("async def build_graph_executor"):]
    assert "memory" not in graph_part.lower().replace("in-memory", "").replace("memory repo", ""), "the graph builder is untouched"


def test_the_session_builder_adds_the_tools_before_resolving_toolsets_and_passes_the_parts_on():
    """The builder augments tools before entering the shared context helper, which derives providers from those augmented tools."""
    root = Path(__file__).resolve().parents[2] / "primer"
    src = (root / "worker" / "executor_builders.py").read_text(encoding="utf-8")
    body = src[src.index("async def build_agent_executor"):src.index("async def build_graph_executor")]
    helper = src[src.index("async def _prepare_agent_tool_manager_context"):src.index("def _construct_agent_tool_manager")]
    assert body.index("with_memory_tools(agent.tools)") < body.index("_prepare_agent_tool_manager_context(")
    assert "_toolset_ids_from_scoped(tools)" in helper
    assert "memory_parts=memory_parts" in body and "memory_index=memory_index" in body


def test_the_frozen_index_key_is_a_system_key():
    from primer.memory.runtime import INDEX_KEY
    from primer.workspace.session_factory import RESERVED_SESSION_METADATA_KEYS

    assert INDEX_KEY in RESERVED_SESSION_METADATA_KEYS, "a caller's session metadata may not set it"
