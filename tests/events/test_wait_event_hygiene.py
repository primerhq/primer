"""T108 (GS-12, GS-13, GS-33, GS-34): a python watch park the watcher can read, no orphaned wait subscriptions, fan-out siblings that do not
share a wake key, and cursors seeded where an in-flight commit cannot fall below them."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from primer.bus.watch_notify import path_matches_watch
from primer.bus.watcher import _extract_park_dict
from primer.events.dispatcher import EventDispatcher
from primer.events.memory_store import InMemoryEventStore
from primer.graph._node_identity import reset_current_graph_node_id, set_current_graph_node_id
from primer.model.event import Event, EventSubscription, LogSink
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.yield_ import ToolContext, Yielded, YieldCancelled, YieldTimeout
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.events_wait import make_wait_for_event_handler, wait_for_event_resume
from primer.toolset.python_runner.yielding import YieldKindError, to_yielded
from primer.worker.yield_resume_registry import ResumeContext, get_resume_hook
from tests.conftest import _FakeStorageProvider

ADMIN = PrincipalRef(type="user", id="user-1", display="u", role="admin", source="local")


def _ctx(call_id="call_0", session="sess-1", workspace="ws1") -> ToolContext:
    return ToolContext(tool_call_id=call_id, session_id=session, workspace_id=workspace, initiated_by=ADMIN)


def _watch(paths, ctx=None, **params):
    return to_yielded({"kind": "watch", "params": {"paths": paths, **params}, "meta": {}}, tool_name="ts__t", ctx=ctx or _ctx(), source_version=1)


# --- GS-12: the python watch park ------------------------------------------

def test_a_python_watch_park_carries_its_paths_where_the_watcher_reads_them():
    yielded = _watch(["notes.txt", "docs"], batch_window_ms=250)
    session = SimpleNamespace(parked_event_key=yielded.event_key, workspace_id="ws1", parked_state={"yielded": yielded.to_jsonable()})
    park = _extract_park_dict(session)
    assert park["paths"] == ["notes.txt", "docs"] and park["batch_window_ms"] == 250
    assert yielded.resume_metadata["workspace_id"] == "ws1"
    assert any(path_matches_watch("notes.txt", w) for w in park["paths"])


def test_a_python_watch_defaults_its_batch_window():
    yielded = _watch(["a.txt"])
    assert "batch_window_ms" in yielded.resume_metadata and yielded.resume_metadata["paths"] == ["a.txt"]


@pytest.mark.parametrize("bad", ["/etc/passwd", "../up.txt", "a/../../b", "C:/x", "a\\..\\b"])
def test_a_python_watch_refuses_absolute_and_traversing_paths(bad):
    with pytest.raises(YieldKindError):
        _watch([bad])


def test_a_python_watch_without_a_workspace_is_refused():
    with pytest.raises(YieldKindError):
        _watch(["a.txt"], ctx=_ctx(workspace=None))


# --- GS-33: keys of fan-out siblings ---------------------------------------

def _under_node(node_id):
    return set_current_graph_node_id(node_id)


def test_python_watch_keys_of_two_siblings_with_one_provider_call_id_differ():
    keys = []
    for node in ("n1#0", "n1#1"):
        entered = _under_node(node)
        try:
            keys.append(_watch(["a.txt"]).event_key)
        finally:
            reset_current_graph_node_id(entered)
    assert keys[0] != keys[1]
    assert _watch(["a.txt"]).event_key == "watch:sess-1:call_0"           # outside a graph: unchanged


async def test_builtin_watch_and_wait_keys_fold_in_the_node_and_stay_put_outside_a_graph():
    from primer.toolset import workspaces as ws_mod

    sp = _FakeStorageProvider()
    wait = make_wait_for_event_handler(sp)
    keys = {"watch": [], "wait": []}
    for node in (None, "n1#0", "n1#1"):
        entered = _under_node(node) if node else None
        try:
            w = await ws_mod._watch_files_handler({"paths": ["a.txt"]}, ctx=_ctx())
            e = await wait({"event_types": ["agent.updated"]}, ctx=_ctx())
            keys["watch"].append(w.event_key)
            keys["wait"].append(e.event_key)
        finally:
            if entered:
                reset_current_graph_node_id(entered)
    assert keys["watch"][0] == "watch:sess-1:call_0" and keys["wait"][0] == "evwait:sess-1:call_0"
    assert len(set(keys["watch"])) == 3 and len(set(keys["wait"])) == 3


# --- GS-13: no orphaned wait subscriptions ---------------------------------

@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    yield provider
    await provider.aclose()


async def _left(sp):
    return (await sp.get_storage(EventSubscription).list(OffsetPage(offset=0, length=200))).items


@pytest.mark.parametrize("ending", [YieldTimeout(elapsed_seconds=60.0), YieldCancelled(reason="stop", cancelled_at=datetime.now(UTC), elapsed_seconds=2.0)])
async def test_a_timed_out_or_cancelled_wait_leaves_no_subscription(sp, ending):
    handler = make_wait_for_event_handler(sp)
    hook = get_resume_hook("wait_for_event")
    for i in range(20):
        ctx = _ctx(call_id=f"call-{i}")
        y = await handler({"event_types": ["session.ended"], "timeout_seconds": 60}, ctx=ctx)
        assert isinstance(y, Yielded)
        out = hook(y.resume_metadata, ending, ResumeContext(tool_name="wait_for_event", tool_call_id=ctx.tool_call_id, session_id="sess-1"))
        out = await out if hasattr(out, "__await__") else out
        assert json.loads(out.output).get("timed_out") or json.loads(out.output).get("cancelled")
    assert await _left(sp) == []
    assert await sp.get_event_store().get_cursor("anything") is None


async def test_a_delivered_wait_result_is_unchanged_and_needs_no_storage():
    envelope = {"event_type": "agent.updated", "payload": {"x": 1}}
    out = wait_for_event_resume({"event_types": ["agent.updated"]}, envelope, None)
    assert json.loads(out.output) == {"event": envelope}
    timed = json.loads(wait_for_event_resume({"event_types": ["a"]}, YieldTimeout(elapsed_seconds=3.0), None).output)
    assert timed == {"timed_out": True, "elapsed_seconds": 3.0, "event_types": ["a"]}


# --- GS-34: the cursor a new wait starts from ------------------------------

class _HorizonStore(InMemoryEventStore):
    def __init__(self):
        super().__init__()
        self.xmin, self.xmax = 100, 100

    def commit(self, event_id: int, *, age_s: float = 0.0) -> None:
        self._events.append(Event(id=event_id, event_type="t.x", occurred_at=datetime.now(UTC) - timedelta(seconds=age_s), payload={}))
        self._events.sort(key=lambda e: e.id)
        self._next_id = max(self._next_id, event_id + 1)

    async def read_horizon(self):
        return self.xmin, self.xmax


async def test_stable_head_stops_below_a_gap_a_running_transaction_may_fill():
    from primer.events.stable_head import stable_head

    store = _HorizonStore()
    store.commit(1)
    store.commit(3)
    store.xmin, store.xmax = 100, 105           # the owner of id 2 is running
    assert await stable_head(store) == 1
    store.xmin, store.xmax = 105, 105           # nothing is running: the gap is a rollback and permanent
    assert await stable_head(store) == 3


async def test_stable_head_ignores_an_old_gap_and_a_store_without_a_horizon():
    from primer.events.stable_head import stable_head

    store = _HorizonStore()
    store.commit(1, age_s=3600)
    store.commit(3, age_s=3600)
    store.xmin, store.xmax = 100, 105
    assert await stable_head(store) == 3        # the dispatcher itself passes a gap that old
    plain = InMemoryEventStore()
    await plain.append(event_type="t.x")
    assert await stable_head(plain) == 1
    assert await stable_head(InMemoryEventStore()) == 0


async def test_an_event_committing_below_the_wait_cursor_is_delivered():
    store = _HorizonStore()
    sp = _FakeStorageProvider()
    sp._event_store = store
    store.commit(1)
    store.commit(3)                             # id 2 is held by a transaction still running
    store.xmin, store.xmax = 100, 105
    y = await make_wait_for_event_handler(sp)({"event_types": ["t.x"]}, ctx=_ctx())
    sub_id = y.resume_metadata["subscription_id"]
    assert await store.get_cursor(sub_id) == 1
    delivered: list[int] = []

    class _D(EventDispatcher):
        async def _deliver(self, sub, event):      # type: ignore[override]
            delivered.append(event.id)
            return True

    store.commit(2)
    store.xmin, store.xmax = 105, 105
    d = _D(storage_provider=sp)
    await d.drain_once()
    assert 2 in delivered
