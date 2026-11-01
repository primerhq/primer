"""An agent's memory collection through the tools (agent memory Task 2): left out of every list, read through the agent's access, read-only to
every generic write door (`type=forbidden`, `memory_collection_read_only`).

Reuses the access suite's world (ALICE, BOB, CAROL, DAVE, ROOT); the agent ``agent-m`` is owned by ALICE, read by BOB, full for CAROL.
"""

from __future__ import annotations

import pytest

from primer.knowledge.tree import DocumentTreeService
from primer.memory.collection import memory_collection_id
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection, Document
from tests.toolset.test_collection_tools_access import (  # noqa: F401 - env is a fixture of that module
    _FULL, _OWNER, _PAGE, _READ, ALICE, BOB, CAROL, CID, DAVE, ROOT, env,
)

AGENT = "agent-m"
MID = memory_collection_id(AGENT)
CODE = "memory_collection_read_only"
# The sharing doors of the collection ROW (its own access block) are not part of this: it has none.
_ROW_ACCESS = {"get_entity_access", "set_entity_access", "get_collection", "refresh_collection"}


def _swap(value, env):
    if isinstance(value, str):
        return {"{doc}": env.mem_doc, "{doc_body}": env.mem_doc_body}.get(value, MID if value == CID else value)
    if isinstance(value, dict):
        return {k: _swap(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_swap(v, env) for v in value]
    return value


@pytest.fixture
async def memory(env):  # noqa: F811
    sp = env.sp
    await sp.get_storage(Agent).create(Agent(
        id=AGENT, description="a", model=AgentModel(profile_id="p"), access=Access(owners=[ALICE], grants={BOB: "read", CAROL: "full"}),
    ))
    await sp.get_storage(Collection).create(Collection(id=MID, description="memory", memory_of=AGENT))
    tree = DocumentTreeService(sp)
    for parent, slug in (("", "a"), ("a", "kid"), ("", "b")):
        await tree.create(collection_id=MID, parent=parent, slug=slug, body=f"body {slug}")
    doc = await tree.resolve(collection_id=MID, path="b")
    env.mem_doc, env.mem_doc_body = doc.id, {**doc.model_dump(mode="json"), "title": "t"}
    return env


def _doors(doors):
    return [(ts, tool, args) for ts, tool, args in doors if tool not in _ROW_ACCESS]


def _ids(doors) -> list[str]:
    return [f"{ts}:{tool}" for ts, tool, _ in doors]


def _passed_the_gate(is_err: bool, body: dict) -> bool:
    return not is_err or body.get("type") not in ("not-found", "forbidden")


async def test_memory_collection_is_absent_from_every_tool_list(memory) -> None:
    for uid in (ALICE, BOB, ROOT):
        _, body = await memory.call("collections", "collections_list", {}, uid)
        assert MID not in [c["id"] for c in body["collections"]] and CID in [c["id"] for c in body["collections"]], uid
        _, body = await memory.call("system", "list_collections", _PAGE, uid)
        assert MID not in [c["id"] for c in body["items"]], uid
        _, body = await memory.call("system", "find_collections", {"predicate": None, **_PAGE}, uid)
        assert MID not in [c["id"] for c in body["items"]], uid


@pytest.mark.parametrize("uid", [BOB, CAROL, ALICE, ROOT])
@pytest.mark.parametrize(("ts", "tool", "args"), _doors(_READ), ids=_ids(_doors(_READ)))
async def test_a_person_with_read_on_the_agent_reads_by_id(memory, ts, tool, args, uid) -> None:
    is_err, body = await memory.call(ts, tool, _swap(args, memory), uid)
    assert _passed_the_gate(is_err, body), (uid, body)


@pytest.mark.parametrize(("ts", "tool", "args"), _doors(_READ), ids=_ids(_doors(_READ)))
async def test_a_stranger_gets_not_found(memory, ts, tool, args) -> None:
    is_err, body = await memory.call(ts, tool, _swap(args, memory), DAVE)
    assert is_err and body["type"] == "not-found", body


@pytest.mark.parametrize("uid", [ALICE, CAROL, ROOT, BOB])
@pytest.mark.parametrize(("ts", "tool", "args"), _doors(_FULL + _OWNER), ids=_ids(_doors(_FULL + _OWNER)))
async def test_every_generic_write_door_refuses(memory, ts, tool, args, uid) -> None:
    is_err, body = await memory.call(ts, tool, _swap(args, memory), uid)
    assert is_err and body["type"] == "forbidden" and CODE in body["message"], (uid, body)
    assert (await memory.sp.get_storage(Document).get(memory.mem_doc)) is not None
    assert (await memory.sp.get_storage(Collection).get(MID)).description == "memory"
