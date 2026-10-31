"""Collections and documents in tools act with the person's access (access-control plan P3 Task 2, Decision 6).

The same levels as the REST routes: read = list, read, tree, search; full = write, move, delete documents; owner = delete
the collection, share it, re-index it. A collection the caller cannot read answers exactly like a missing one
(``type=not-found``); too low a level answers ``type=forbidden``. The person is the run's initiator, or the MCP request's
actor when the call carries no context. The generic ``get_document`` / ``find_documents`` and the collection lists only
reach readable collections; the wiki tools pass the caller along and keep working because ``system`` is public.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio

from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID, regenerate_system_collection
from primer.knowledge.tree import DocumentTreeService
from primer.mcp.server import current_actor
from primer.model.access import Access
from primer.model.collection import Collection, Document
from primer.model.principal import Principal, PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.collections import build_collections_toolset
from primer.toolset.system import build_system_toolset
from tests.toolset.test_wiki_tools import _Registry

ALICE, BOB, CAROL, DAVE, ROOT = "alice", "bob", "carol", "dave", "root"
CID = "kb"
_PAGE = {"offset": 0, "limit": 50}


def _ctx(uid: str) -> ToolContext:
    role = "admin" if uid == ROOT else "user"
    who = PrincipalRef(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
    return ToolContext(tool_call_id="tc", session_id="s", workspace_id="w", initiated_by=who)


class _Env:
    def __init__(self, sp: SqliteStorageProvider, colls: Any, system: Any) -> None:
        self.sp, self.colls, self.system = sp, colls, system

    async def call(self, toolset: str, tool: str, args: dict, uid: str | None) -> tuple[bool, dict]:
        provider = self.colls if toolset == "collections" else self.system
        r = await provider.call(tool_name=tool, arguments=args, ctx=_ctx(uid) if uid else None)
        return r.is_error, json.loads(r.output)

    async def doc_id(self, path: str = "a") -> str:
        return (await DocumentTreeService(self.sp).resolve(collection_id=CID, path=path)).id


@pytest_asyncio.fixture
async def env(tmp_path) -> AsyncIterator[_Env]:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    for uid in (ALICE, BOB, CAROL, DAVE, ROOT):
        await sp.get_storage(User).create(
            User(id=uid, username=uid, created_at=datetime.now(UTC), role="admin" if uid == ROOT else "user")
        )
    await sp.get_storage(Collection).create(
        Collection(id=CID, description="wiki", access=Access(owners=[ALICE], grants={BOB: "read", CAROL: "full"}))
    )
    tree = DocumentTreeService(sp)
    for parent, slug in (("", "a"), ("a", "kid"), ("", "b")):
        await tree.create(collection_id=CID, parent=parent, slug=slug, body=f"body {slug}")
    colls = build_collections_toolset(storage_provider=sp)
    system = build_system_toolset(storage_provider=sp, provider_registry=_Registry(colls))
    yield _Env(sp, colls, system)
    await sp.aclose()


# (toolset, tool, args); a "{doc}" value is replaced by the id of document "b".
_READ = [
    ("collections", "collection_tree", {"collection": CID}),
    ("collections", "read_document", {"collection": CID, "path": "a"}),
    ("collections", "search", {"collection": CID, "query": "body", "mode": "literal"}),
    ("system", "get_collection", {"id": CID}),
    ("system", "get_entity_access", {"kind": "collection", "id": CID}),
    ("system", "list_collection_documents", {"collection_id": CID, **_PAGE}),
    ("system", "find_collection_documents_by_meta", {"collection_id": CID, **_PAGE}),
    ("system", "search_collection", {"collection_id": CID, "query": "q"}),
    ("system", "get_document_content", {"collection_id": CID, "path": "a"}),
    ("system", "list_documents", {"collection_id": CID}),
    ("system", "get_document", {"id": "{doc}"}),
]
_FULL = [
    ("collections", "create_document", {"collection": CID, "parent": "", "slug": "new", "body": "n"}),
    ("collections", "update_document", {"collection": CID, "path": "a", "body": "edited"}),
    ("collections", "move_document", {"collection": CID, "path": "b", "new_parent": "a"}),
    ("collections", "delete_document", {"collection": CID, "path": "b"}),
    ("system", "update_collection", {"id": CID, "entity": {"id": CID, "description": "renamed"}}),
    ("system", "put_document", {"collection_id": CID, "path": "x", "content": "x"}),
    ("system", "move_document", {"collection_id": CID, "from": "b", "to": "c"}),
    ("system", "create_document", {"entity": {"collection_id": CID, "slug": "n2", "path": "n2"}}),
    ("system", "update_document", {"id": "{doc}", "entity": "{doc_body}"}),
    ("system", "delete_document", {"id": "{doc}"}),
]
_OWNER = [
    ("system", "delete_collection", {"id": CID}),
    ("system", "set_entity_access", {"kind": "collection", "id": CID, "owners": [ALICE], "public": True}),
    ("system", "refresh_collection", {"collection_id": CID}),
]


def _ids(doors) -> list[str]:
    return [f"{ts}:{tool}" for ts, tool, _ in doors]


async def _args(env: _Env, args: dict) -> dict:
    doc = await env.sp.get_storage(Document).get(await env.doc_id("b"))
    swap = {"{doc}": doc.id, "{doc_body}": {**doc.model_dump(mode="json"), "title": "t"}}
    return {k: swap.get(v, v) if isinstance(v, str) else v for k, v in args.items()}


def _passed_the_gate(is_err: bool, body: dict) -> bool:
    """The tool ran past the access check: a success, or a refusal that is not about access."""
    return not is_err or body.get("type") not in ("not-found", "forbidden")


@pytest.mark.parametrize(("ts", "tool", "args"), _READ + _FULL + _OWNER, ids=_ids(_READ + _FULL + _OWNER))
async def test_a_private_collection_is_invisible_to_a_stranger(env, ts, tool, args):
    is_err, body = await env.call(ts, tool, await _args(env, args), DAVE)
    assert is_err and body["type"] == "not-found", body
    assert "does not exist" in body["message"]


@pytest.mark.parametrize(("ts", "tool", "args"), _READ, ids=_ids(_READ))
async def test_a_reader_reads(env, ts, tool, args):
    is_err, body = await env.call(ts, tool, await _args(env, args), BOB)
    assert _passed_the_gate(is_err, body), body


@pytest.mark.parametrize(("ts", "tool", "args"), _FULL + _OWNER, ids=_ids(_FULL + _OWNER))
async def test_a_reader_cannot_write(env, ts, tool, args):
    is_err, body = await env.call(ts, tool, await _args(env, args), BOB)
    assert is_err and body["type"] == "forbidden", body


@pytest.mark.parametrize(("ts", "tool", "args"), _READ + _FULL, ids=_ids(_READ + _FULL))
async def test_a_full_grantee_writes_documents(env, ts, tool, args):
    is_err, body = await env.call(ts, tool, await _args(env, args), CAROL)
    assert _passed_the_gate(is_err, body), body


@pytest.mark.parametrize(("ts", "tool", "args"), _OWNER, ids=_ids(_OWNER))
async def test_a_full_grantee_cannot_delete_share_or_reindex(env, ts, tool, args):
    is_err, body = await env.call(ts, tool, await _args(env, args), CAROL)
    assert is_err and body["type"] == "forbidden", body


@pytest.mark.parametrize("uid", [ALICE, ROOT])
@pytest.mark.parametrize(("ts", "tool", "args"), _READ + _FULL + _OWNER, ids=_ids(_READ + _FULL + _OWNER))
async def test_the_owner_and_an_admin_pass_every_door(env, ts, tool, args, uid):
    is_err, body = await env.call(ts, tool, await _args(env, args), uid)
    assert _passed_the_gate(is_err, body), body


async def test_the_lists_only_reach_readable_collections(env):
    await env.sp.get_storage(Collection).create(Collection(id="pub", description="d", access=Access(owners=[ALICE], public=True)))
    _, body = await env.call("collections", "collections_list", {}, DAVE)
    assert [c["id"] for c in body["collections"]] == ["pub"]
    _, body = await env.call("system", "list_collections", _PAGE, DAVE)
    assert [c["id"] for c in body["items"]] == ["pub"]
    _, body = await env.call("system", "find_collections", {"predicate": None, **_PAGE}, DAVE)
    assert [c["id"] for c in body["items"]] == ["pub"]
    _, body = await env.call("collections", "collections_list", {}, BOB)
    assert [c["id"] for c in body["collections"]] == [CID, "pub"]
    _, body = await env.call("collections", "collections_list", {}, ROOT)
    assert [c["id"] for c in body["collections"]] == [CID, "pub"]


async def test_find_documents_only_reaches_readable_collections(env):
    by_coll = {"kind": "predicate", "left": {"kind": "field", "name": "collection_id"}, "op": "=", "right": {"kind": "value", "value": CID}}
    for predicate in (None, by_coll):
        is_err, body = await env.call("system", "find_documents", {"predicate": predicate, **_PAGE}, DAVE)
        assert not is_err and body["items"] == [], body
    _, body = await env.call("system", "find_documents", {"predicate": None, **_PAGE}, BOB)
    assert len(body["items"]) == 3
    _, body = await env.call("system", "find_documents", {"predicate": None, **_PAGE}, ROOT)
    assert len(body["items"]) == 3


async def test_a_create_or_update_body_cannot_set_access(env):
    is_err, body = await env.call(
        "system", "create_collection",
        {"entity": {"id": "mine", "description": "d", "access": {"owners": [ALICE], "public": True}}}, DAVE,
    )
    assert not is_err, body
    stored = await env.sp.get_storage(Collection).get("mine")
    assert stored.access.owners == [DAVE] and stored.access.public is False
    is_err, body = await env.call(
        "system", "update_collection",
        {"id": CID, "entity": {"id": CID, "description": "x", "access": {"owners": [CAROL], "public": True}}}, CAROL,
    )
    assert not is_err, body
    after = (await env.sp.get_storage(Collection).get(CID)).access
    assert after.owners == [ALICE] and after.grants == {BOB: "read", CAROL: "full"} and after.public is False


async def test_the_owner_shares_through_the_tool_and_the_grantee_then_reads(env):
    is_err, body = await env.call(
        "system", "set_entity_access", {"kind": "collection", "id": CID, "owners": [ALICE], "grants": {DAVE: "read"}}, ALICE,
    )
    assert not is_err, body
    is_err, body = await env.call("collections", "read_document", {"collection": CID, "path": "a"}, DAVE)
    assert not is_err and body["body"] == "body a", body


async def test_the_system_collection_reads_for_everyone_and_refuses_their_writes(env):
    await regenerate_system_collection(env.sp, toolset_providers={})
    is_err, body = await env.call("collections", "collection_tree", {"collection": SYSTEM_COLLECTION_ID}, DAVE)
    assert not is_err, body
    is_err, body = await env.call("system", "put_document", {"collection_id": SYSTEM_COLLECTION_ID, "path": "x", "content": "x"}, DAVE)
    assert is_err and body["type"] == "forbidden", body
    is_err, body = await env.call("system", "put_document", {"collection_id": SYSTEM_COLLECTION_ID, "path": "x", "content": "x"}, ROOT)
    assert is_err and body["type"] == "forbidden" and "system-owned" in body["message"], body


async def test_the_wiki_tools_act_as_the_caller(env):
    await regenerate_system_collection(env.sp, toolset_providers={})
    args = {"query": "install", "mode": "fulltext", "path_prefix": "this-install"}
    is_err, body = await env.call("system", "wiki_search", args, DAVE)
    assert not is_err and body["hits"], body
    is_err, body = await env.call("system", "wiki_read", {"path": "this-install"}, DAVE)
    assert not is_err, body
    # Were the wiki private, a plain user would read nothing of it; an admin still would.
    await env.sp.get_storage(Collection).patch_if(SYSTEM_COLLECTION_ID, {"access": Access().model_dump(mode="json")}, where={"system": [True]})
    is_err, body = await env.call("system", "wiki_search", args, DAVE)
    assert is_err and body["type"] == "not-found", body
    is_err, body = await env.call("system", "wiki_read", {"path": "this-install"}, DAVE)
    assert is_err and body["type"] == "not-found", body
    is_err, body = await env.call("system", "wiki_search", args, ROOT)
    assert not is_err and body["hits"], body


async def test_an_mcp_call_acts_as_the_request_actor(env):
    for uid, readable in ((DAVE, False), (BOB, True)):
        token = current_actor.set(Principal(type="api_token", id="tok", display="t", role="user", source="local", user_id=uid))
        try:
            is_err, body = await env.call("collections", "read_document", {"collection": CID, "path": "a"}, None)
        finally:
            current_actor.reset(token)
        assert is_err is not readable, body
    # Nobody known reads only public rows.
    is_err, body = await env.call("collections", "read_document", {"collection": CID, "path": "a"}, None)
    assert is_err and body["type"] == "not-found", body
