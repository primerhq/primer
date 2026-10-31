"""The graph-run (graph thread) CRUD tools inherit their graph's access (final review F11): read to see a run, full to change it."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from primer.model.access import Access
from primer.model.graph import Graph, GraphThread, NodeRuntimeState
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.system import build_system_toolset

NOW = datetime(2026, 10, 31, tzinfo=UTC)
ALICE, BOB, CAROL, ROOT = "alice", "bob", "carol", "root"


def _ctx(uid: str) -> ToolContext:
    ref = PrincipalRef(type="user", id=uid, display=uid, role="admin" if uid == ROOT else "user", source="local", user_id=uid)
    return ToolContext(tool_call_id="tc-1", session_id=None, workspace_id=None, initiated_by=ref)


@pytest.fixture
async def world(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    for uid in (ALICE, BOB, CAROL, ROOT):
        await sp.get_storage(User).create(User(id=uid, username=uid, created_at=NOW, role="admin" if uid == ROOT else "user"))
    await sp.get_storage(Graph).create(Graph(id="g-private", description="alice only", nodes=[], edges=[], access=Access.owned_by(ALICE)))
    await sp.get_storage(Graph).create(Graph(id="g-shared", description="shared", nodes=[], edges=[], access=Access(owners=[ALICE], grants={CAROL: "read"})))
    for gid in ("g-private", "g-shared"):
        await sp.get_storage(GraphThread).create(GraphThread(
            id=f"gt-{gid}", graph_id=gid, title=gid, created_at=NOW, last_activity_at=NOW,
            node_states={"n": NodeRuntimeState(error="db password rejected for alice@corp")},
        ))
    yield sp, build_system_toolset(storage_provider=sp, provider_registry=MagicMock())
    await sp.aclose()


def _body(res):
    return json.loads(res.output)


async def test_a_stranger_neither_lists_reads_edits_nor_deletes_a_private_graphs_run(world):
    sp, system = world
    bob = _ctx(BOB)
    listed = await system.call(tool_name="list_graph_threads", arguments={}, ctx=bob)
    assert not listed.is_error and _body(listed)["total"] == 0
    got = await system.call(tool_name="get_graph_thread", arguments={"id": "gt-g-private"}, ctx=bob)
    assert got.is_error and "secret" not in got.output and "alice@corp" not in got.output
    upd = await system.call(tool_name="update_graph_thread", arguments={"id": "gt-g-private", "entity": {"id": "gt-g-private", "graph_id": "g-private", "title": "x"}}, ctx=bob)
    dele = await system.call(tool_name="delete_graph_thread", arguments={"id": "gt-g-private"}, ctx=bob)
    assert upd.is_error and dele.is_error
    assert (await sp.get_storage(GraphThread).get("gt-g-private")).title == "g-private"


async def test_a_reader_of_the_graph_reads_but_cannot_change_its_run(world):
    sp, system = world
    carol = _ctx(CAROL)
    listed = await system.call(tool_name="list_graph_threads", arguments={}, ctx=carol)
    assert [t["id"] for t in _body(listed)["items"]] == ["gt-g-shared"]
    assert not (await system.call(tool_name="get_graph_thread", arguments={"id": "gt-g-shared"}, ctx=carol)).is_error
    assert (await system.call(tool_name="delete_graph_thread", arguments={"id": "gt-g-shared"}, ctx=carol)).is_error
    assert await sp.get_storage(GraphThread).get("gt-g-shared") is not None


async def test_an_admin_reaches_every_run(world):
    _, system = world
    listed = await system.call(tool_name="list_graph_threads", arguments={}, ctx=_ctx(ROOT))
    assert _body(listed)["total"] == 2
