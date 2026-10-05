"""The park-batch membership helper over the three real ``parked_state`` shapes (plan 3.3, the lead's ruling C1).

``batches_referenced_by_park(blob) -> dict[str, BatchRef]`` names every BATCH a park references, keyed on the batch's
first stored id (``outstanding[0]``, else ``notifying[0]``) exactly as stored, i.e. session-qualified. A batch is one
``graph_checkpoint['pending_tool_waits']`` entry for a graph park (the classic ``ParkedState`` blob's mixed park, which
has no top-level ``kind``, and the pure tool_wait blob's own checkpoint), or the single top-level batch of a tool_wait
blob with NO ``graph_checkpoint`` (the agent surface). A pure graph tool_wait blob also carries top-level lists
FLATTENED across its nodes; they are a projection of the entries and never a batch.

The graph blobs here are the real ones: the real ``GraphExecutor`` parks two fan-out siblings in one superstep (or one
beside a human gate) and the real ``run_one_session_turn`` park arms store them, qualifying the ids and adding the
flattened lists. The agent-surface shapes the turn loop cannot produce (a batch with no outstanding id) come from
``ToolWaitParkedState.to_jsonable``. Every blob goes through JSON first, because the helper reads what the row stores.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind, Lease
from primer.model.chat import ExtendedEvent, ToolCallEnd, ToolCallStart, ToolResultPart, _GraphNodeEvent
from primer.model.tool_call_task import tool_call_task_id
from primer.model.workspace_session import WorkspaceSession
from primer.model.yield_ import ToolWaitPark, Yielded, YieldToWorker
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.tool_wait_batches import BatchRef, batches_referenced_by_park
from primer.worker.yield_runtime import ParkedState, ToolWaitParkedState

from tests.conftest import _FakeStorageProvider
from tests.graph.test_tool_wait_graph_park import _ask_user_yield, _mk_parallel_executor, _patch_run_agent_turn
from tests.session.test_dispatch_park_arms_e2e import (
    _FakeEventBus,
    _FakeWorkspaceIO,
    _RecordingClaimEngine,
    _session,
)


def _ref(node_id, outstanding, notifying) -> BatchRef:
    return BatchRef(node_id=node_id, outstanding_ids=tuple(outstanding), notifying_ids=tuple(notifying))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_stored(blob: dict) -> dict:
    """What the row holds: the park blob after its JSON round trip (tuples become lists)."""
    return json.loads(json.dumps(blob))


def _wrap_node(node_id: str, inner):
    """A node's stream event as the graph executor forwards it (the turn loop mints the scoped id per node)."""
    return ExtendedEvent(
        extended=_GraphNodeEvent(
            node_id=node_id, iteration=0, inner_type=inner.type, inner_payload=inner.model_dump(mode="json"),
        )
    )


class _ReplaysCallsThenRaises:
    """Streams one ToolCallStart/End per ``(node_id, raw_id)`` (so the turn loop records each call under the scoped id
    the graph minted for it) and then raises the park the REAL graph executor produced."""

    _tool_calls_as_claims_enabled = True

    def __init__(self, calls: list[tuple[str | None, str]], park: BaseException) -> None:
        self._calls = calls
        self._park = park

    async def invoke(self, messages, **kwargs):
        for node_id, raw_id in self._calls:
            start = ToolCallStart(id=raw_id, name="tool_x", index=0)
            end = ToolCallEnd(id=raw_id, arguments={}, index=0)
            yield start if node_id is None else _wrap_node(node_id, start)
            yield end if node_id is None else _wrap_node(node_id, end)
        raise self._park
        yield  # pragma: no cover - unreachable, keeps this a generator


async def _stored_park(session_id: str, calls: list[tuple[str | None, str]], park: BaseException) -> dict:
    """Run the real turn loop over ``park`` and return the ``parked_state`` its park arm stores."""
    storage_provider = _FakeStorageProvider()
    await storage_provider.get_storage(WorkspaceSession).create(_session(session_id))
    executor = _ReplaysCallsThenRaises(calls, park)

    async def _build_executor(_session: WorkspaceSession):
        return executor

    deps = SessionDispatchDeps(
        storage_provider=storage_provider, workspace_io=_FakeWorkspaceIO(), event_bus=_FakeEventBus(),
        build_executor=_build_executor, claim_engine=_RecordingClaimEngine(),
    )
    lease = Lease(
        kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1",
        claimed_at=_now(), expires_at=_now(), attempt_count=0, last_error=None,
    )
    outcome = await run_one_session_turn(lease, deps)
    assert outcome.success is True and outcome.park is not None, outcome
    return _as_stored(outcome.park.parked_state)


async def _real_graph_park(monkeypatch, behavior: dict) -> BaseException:
    """The park the real GraphExecutor raises when its two parallel nodes A and B behave as ``behavior`` says."""
    executor = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, behavior)
    with pytest.raises((ToolWaitPark, YieldToWorker)) as excinfo:
        async for _ev in executor.invoke([]):
            pass
    return excinfo.value


def _node_park(outstanding: list[str], notifying: list[str] = (), *, graph_node: bool = True) -> ToolWaitPark:
    """What the agent loop raises for one batch (scoped ids, as it holds them). A graph node's park carries its
    message dicts into its checkpoint entry; the agent surface's turn loop leaves ``llm_messages`` to the executor."""
    return ToolWaitPark(
        outstanding_task_ids=list(outstanding),
        event_key=f"tool_wait:{outstanding[0]}",
        notifying_results=[(i, ToolResultPart(id=i, output="answered inline")) for i in notifying],
        llm_messages=(
            [{"role": "assistant", "parts": [{"type": "text", "text": "dispatching"}]}] if graph_node else None
        ),
    )


async def _pure_graph_blob(monkeypatch, session_id: str) -> dict:
    """Node A parks two claimable calls, node B one claimable call and one notifying call, in ONE superstep."""
    park = await _real_graph_park(monkeypatch, {
        "agent-a": _node_park(["A:tool:0:1", "A:tool:0:2"]),
        "agent-b": _node_park(["B:tool:0:1"], ["B:tool:0:2"]),
    })
    assert isinstance(park, ToolWaitPark)
    calls = [("A", "call_a1"), ("A", "call_a2"), ("B", "call_b1"), ("B", "call_b2")]
    return await _stored_park(session_id, calls, park)


async def _mixed_graph_blob(monkeypatch, session_id: str) -> dict:
    """Node A parks a batch of two claimable calls while node B waits on a human (ask_user): a classic mixed park."""
    park = await _real_graph_park(monkeypatch, {
        "agent-a": _node_park(["A:tool:0:1", "A:tool:0:2"]),
        "agent-b": _ask_user_yield("B", "tc-b"),
    })
    assert isinstance(park, YieldToWorker)
    return await _stored_park(session_id, [("A", "call_a1"), ("A", "call_a2")], park)


def _agent_blob(session_id: str, outstanding: list[str], notifying: list[str]) -> dict:
    return _as_stored(ToolWaitParkedState(
        outstanding_task_ids=[tool_call_task_id(session_id, i) for i in outstanding],
        notifying_task_ids=[tool_call_task_id(session_id, i) for i in notifying],
        event_key=f"tool_wait:{session_id}:0:x",
        llm_messages=[],
        turn_no=0,
        started_at=_now(),
    ).to_jsonable())


# ---------------------------------------------------------------------------
# The graph shapes
# ---------------------------------------------------------------------------


async def test_a_pure_graph_tool_wait_park_has_one_batch_per_node_and_its_flattened_lists_are_not_one(
    monkeypatch,
) -> None:
    sid = "s-pure"
    blob = await _pure_graph_blob(monkeypatch, sid)
    a1, a2, b1, b2 = (f"{sid}/{i}" for i in ("A:tool:0:1", "A:tool:0:2", "B:tool:0:1", "B:tool:0:2"))
    # The stored blob really carries the cross-node projection the helper must ignore.
    assert blob["kind"] == "tool_wait"
    assert blob["outstanding_task_ids"] == [a1, a2, b1]
    assert blob["notifying_task_ids"] == [b2]
    entries = blob["graph_checkpoint"]["pending_tool_waits"]

    batches = batches_referenced_by_park(blob)

    assert batches == {
        a1: _ref("A", [a1, a2], []),
        b1: _ref("B", [b1], [b2]),
    }
    # The key set is exactly the per-node entries' first ids, as stored.
    assert set(batches) == {e["outstanding_task_ids"][0] for e in entries}
    assert all(ref.node_id is not None for ref in batches.values())


async def test_a_stale_or_reordered_flattened_projection_still_names_no_batch(monkeypatch) -> None:
    """The flattened lists are ignored whatever they say: an id ONLY they carry is referenced by no batch."""
    sid = "s-pure-stale"
    stored = await _pure_graph_blob(monkeypatch, sid)
    parked = ToolWaitParkedState.from_jsonable(stored)
    stray = f"{sid}/Z:tool:0:9"
    blob = _as_stored(dataclasses.replace(
        parked,
        outstanding_task_ids=[stray, *reversed(parked.outstanding_task_ids)],
        notifying_task_ids=[f"{sid}/Z:tool:0:10"],
    ).to_jsonable())

    batches = batches_referenced_by_park(blob)

    assert set(batches) == {f"{sid}/A:tool:0:1", f"{sid}/B:tool:0:1"}
    referenced = {i for ref in batches.values() for i in (*ref.outstanding_ids, *ref.notifying_ids)}
    assert stray not in referenced and f"{sid}/Z:tool:0:10" not in referenced


async def test_a_mixed_park_reads_its_batches_from_the_classic_blobs_checkpoint(monkeypatch) -> None:
    sid = "s-mixed"
    blob = await _mixed_graph_blob(monkeypatch, sid)
    # The classic ParkedState blob: no top-level kind, no top-level batch lists, a human gate beside the batch.
    assert "kind" not in blob and "outstanding_task_ids" not in blob
    assert [e["node_id"] for e in blob["graph_checkpoint"]["pending_agent_yields"]] == ["B"]

    assert batches_referenced_by_park(blob) == {
        f"{sid}/A:tool:0:1": _ref("A", [f"{sid}/A:tool:0:1", f"{sid}/A:tool:0:2"], []),
    }


def test_a_graph_blob_whose_checkpoint_has_no_entries_has_no_batch() -> None:
    """With a ``graph_checkpoint`` the entries are the only batches, so a checkpoint without any names none (the
    tool_wait resume ends such a session failed); its flattened lists do not become an agent batch."""
    sid = "s-empty-ck"
    pure = _as_stored(ToolWaitParkedState(
        outstanding_task_ids=[f"{sid}/A:tool:0:1"], notifying_task_ids=[], event_key=f"tool_wait:{sid}:0:A",
        llm_messages=[], turn_no=0, started_at=_now(), graph_checkpoint={"pending_tool_waits": []},
    ).to_jsonable())
    classic = _as_stored(ParkedState(
        yielded=Yielded(tool_name="ask_user", event_key=f"ask_user:{sid}:tc"), llm_messages=[], turn_no=0,
        started_at=_now(), tool_call_id="tc", graph_checkpoint={"pending_toolcalls": []},
    ).to_jsonable())

    assert batches_referenced_by_park(pure) == {}
    assert batches_referenced_by_park(classic) == {}


# ---------------------------------------------------------------------------
# The agent surface
# ---------------------------------------------------------------------------


async def test_the_agent_surface_is_one_batch_keyed_on_its_first_outstanding_id() -> None:
    """Through the real turn loop: two claimable calls and one answered inline, on an agent-bound session."""
    sid = "s-agent"
    park = _node_park(["x:tool:0:1", "x:tool:0:2"], ["x:tool:0:3"], graph_node=False)
    blob = await _stored_park(sid, [(None, "call_1"), (None, "call_2"), (None, "call_3")], park)
    assert blob["kind"] == "tool_wait" and blob["graph_checkpoint"] is None

    assert batches_referenced_by_park(blob) == {
        f"{sid}/x:tool:0:1": _ref(None, [f"{sid}/x:tool:0:1", f"{sid}/x:tool:0:2"], [f"{sid}/x:tool:0:3"]),
    }


@pytest.mark.parametrize(("outstanding", "notifying", "key"), [
    (["x:tool:0:1"], [], "x:tool:0:1"),
    ([], ["x:tool:0:2", "x:tool:0:3"], "x:tool:0:2"),
    (["x:tool:0:4", "x:tool:0:5"], ["x:tool:0:1"], "x:tool:0:4"),
], ids=["outstanding-only", "notifying-only", "both"])
def test_the_agent_surface_key_is_the_first_outstanding_id_else_the_first_notifying_id(
    outstanding, notifying, key,
) -> None:
    sid = "s-agent-shape"
    q = lambda ids: [tool_call_task_id(sid, i) for i in ids]  # noqa: E731

    assert batches_referenced_by_park(_agent_blob(sid, outstanding, notifying)) == {
        tool_call_task_id(sid, key): _ref(None, q(outstanding), q(notifying)),
    }


def test_an_agent_surface_batch_with_no_ids_is_not_a_batch() -> None:
    assert batches_referenced_by_park(_agent_blob("s-agent-empty", [], [])) == {}


# ---------------------------------------------------------------------------
# Keys are the stored ids; nothing is parsed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stored_id", [
    "x:tool:0:1",                       # a park written before ids were qualified keeps its bare id
    "s-odd/n:1/with.dots:tool:0:1",     # a node id holding ':', '/' and '.'
    "not a scoped id at all",           # parser-free: whatever is stored is the key
])
def test_the_key_is_the_first_id_exactly_as_stored(stored_id) -> None:
    blob = _as_stored(ToolWaitParkedState(
        outstanding_task_ids=[stored_id], notifying_task_ids=[], event_key="tool_wait:any",
        llm_messages=[], turn_no=0, started_at=_now(),
    ).to_jsonable())

    assert batches_referenced_by_park(blob) == {stored_id: _ref(None, [stored_id], [])}


# ---------------------------------------------------------------------------
# No park, foreign and malformed blobs
# ---------------------------------------------------------------------------


def test_no_park_and_a_classic_agent_park_reference_no_batch() -> None:
    classic_agent = _as_stored(ParkedState(
        yielded=Yielded(tool_name="ask_user", event_key="ask_user:s:tc"), llm_messages=[], turn_no=0,
        started_at=_now(), tool_call_id="tc",
    ).to_jsonable())

    assert batches_referenced_by_park(None) == {}
    assert batches_referenced_by_park({}) == {}
    assert batches_referenced_by_park(classic_agent) == {}


def test_top_level_batch_lists_outside_a_tool_wait_blob_are_not_a_batch() -> None:
    """The agent batch belongs to a tool_wait blob: the same lists on a classic (kind-less) or foreign blob name none."""
    lists = {"outstanding_task_ids": ["s/x:tool:0:1"], "notifying_task_ids": ["s/x:tool:0:2"]}

    assert batches_referenced_by_park({**lists}) == {}
    assert batches_referenced_by_park({"kind": "something_else", **lists}) == {}


@pytest.mark.parametrize("blob", [
    "a string",
    5,
    ["s/x:tool:0:1"],
    {"kind": "tool_wait", "outstanding_task_ids": "s/x:tool:0:1"},
    {"kind": "tool_wait", "outstanding_task_ids": None, "notifying_task_ids": 7},
    {"kind": "tool_wait", "outstanding_task_ids": [None, 5, ["s/x:tool:0:1"]]},
    {"kind": "tool_wait", "graph_checkpoint": "not a dict", "outstanding_task_ids": ["s/x:tool:0:1"]},
    {"graph_checkpoint": {"pending_tool_waits": "not a list"}},
    {"graph_checkpoint": {"pending_tool_waits": [None, 5, "x", [], {}]}},
    {"graph_checkpoint": {"pending_tool_waits": [{"node_id": 3, "outstanding_task_ids": {"a": 1}}]}},
    {"graph_checkpoint": {"pending_tool_waits": [{"notifying_results": [None, [], "ab", [5, {}], {"a": 1}]}]}},
], ids=lambda b: type(b).__name__)
def test_a_malformed_blob_does_not_raise_and_names_no_batch(blob) -> None:
    assert batches_referenced_by_park(blob) == {}


def test_a_malformed_entry_does_not_hide_its_well_formed_siblings() -> None:
    blob = {"graph_checkpoint": {"pending_tool_waits": [
        None,
        {"node_id": "A", "outstanding_task_ids": ["s/A:tool:0:1", 5, None], "notifying_results": [[7, {}]]},
        {"node_id": 9, "outstanding_task_ids": [], "notifying_results": [["s/B:tool:0:1", {}], "junk"]},
    ]}}

    assert batches_referenced_by_park(blob) == {
        "s/A:tool:0:1": _ref("A", ["s/A:tool:0:1"], []),
        # a non-string node id is not invented into one; the batch it carries is still referenced
        "s/B:tool:0:1": _ref(None, [], ["s/B:tool:0:1"]),
    }


# ---------------------------------------------------------------------------
# The value type, and purity
# ---------------------------------------------------------------------------


async def test_the_helper_does_not_touch_the_blob_and_batchref_is_a_frozen_value(monkeypatch) -> None:
    blob = await _pure_graph_blob(monkeypatch, "s-pure-value")
    before = copy.deepcopy(blob)

    batches = batches_referenced_by_park(blob)

    assert blob == before
    ref = batches["s-pure-value/B:tool:0:1"]
    assert isinstance(ref.outstanding_ids, tuple) and isinstance(ref.notifying_ids, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.node_id = "C"
    assert hash(ref) == hash(_ref("B", ["s-pure-value/B:tool:0:1"], ["s-pure-value/B:tool:0:2"]))
