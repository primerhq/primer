"""Worker wiring for the unified nested-yield continuation (Task 3.3b).

Focused unit tests for the two thin pool methods that bridge the worker to
the pure continuation walk:

* ``_build_invocation_services`` - asserts the returned InvocationServices'
  closures forward to ``primer.agent.invoke`` with the worker's bound deps
  (storage / provider_registry / approval_resolver) and the session's graph
  services.
* ``_repark_continuation`` - asserts a ``Repark`` outcome is turned into the
  right ParkedState (new frames + new leaf, session turn preserved) and a
  drop-lease ParkRequest listening on the new leaf's event key.
"""

from datetime import datetime, timezone

import primer.agent.invoke as invoke_mod
from primer.model.yield_ import Yielded
from primer.worker.continuation import InvocationServices, Repark
from primer.worker.frames import AgentFrame, AgentResumeContext
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ParkedState


def _bare_pool(*, storage="STORAGE", registry="REGISTRY", approval="APPROVAL"):
    pool = WorkerPool.__new__(WorkerPool)
    pool._storage = storage
    pool._provider_registry = registry
    pool._approval_resolver = approval
    return pool


class _FakeGraphServices:
    def __init__(self):
        self.resolved = None
        self.built = None

    async def resolve_graph(self, graph_id):
        self.resolved = graph_id
        return f"graph::{graph_id}"

    async def build_child_executor(self, *, graph, gsid):
        self.built = (graph, gsid)
        return f"child::{graph}::{gsid}"


class _FakeToolManager:
    def __init__(self, graph_services):
        self._graph_services = graph_services


async def test_build_invocation_services_forwards_bound_deps(monkeypatch):
    captured: dict = {}

    async def fake_build(context, *, storage_provider, provider_registry,
                         approval_resolver):
        captured["build"] = dict(
            context=context, storage_provider=storage_provider,
            provider_registry=provider_registry,
            approval_resolver=approval_resolver,
        )
        return "TOOLMGR"

    async def fake_resume(*, agent_id, context, llm_messages, child_result,
                          depth, storage_provider, provider_registry,
                          approval_resolver, invoke_tool_call_id):
        captured["resume"] = dict(
            agent_id=agent_id, context=context, llm_messages=llm_messages,
            child_result=child_result, depth=depth,
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            approval_resolver=approval_resolver,
            invoke_tool_call_id=invoke_tool_call_id,
        )
        return "RESUMED-TEXT"

    monkeypatch.setattr(invoke_mod, "build_subagent_toolmanager", fake_build)
    monkeypatch.setattr(invoke_mod, "resume_subagent", fake_resume)

    pool = _bare_pool()
    graph_services = _FakeGraphServices()
    tm = _FakeToolManager(graph_services)

    services = pool._build_invocation_services(
        "session", "workspace", "executor", tm,
    )
    assert isinstance(services, InvocationServices)

    out = await services.build_subagent_toolmanager("CTX")
    assert out == "TOOLMGR"
    assert captured["build"]["context"] == "CTX"
    assert captured["build"]["storage_provider"] == "STORAGE"
    assert captured["build"]["provider_registry"] == "REGISTRY"
    assert captured["build"]["approval_resolver"] == "APPROVAL"

    text = await services.resume_subagent(
        agent_id="a1", context="CTX2", llm_messages=[{"role": "assistant"}],
        child_result="CHILD", depth=2, invoke_tool_call_id="tc9",
    )
    assert text == "RESUMED-TEXT"
    assert captured["resume"]["agent_id"] == "a1"
    assert captured["resume"]["child_result"] == "CHILD"
    assert captured["resume"]["depth"] == 2
    assert captured["resume"]["invoke_tool_call_id"] == "tc9"
    assert captured["resume"]["storage_provider"] == "STORAGE"
    assert captured["resume"]["provider_registry"] == "REGISTRY"
    assert captured["resume"]["approval_resolver"] == "APPROVAL"

    # Graph callables thread through the session's GraphInvocationServices.
    assert await services.resolve_graph("g1") == "graph::g1"
    assert graph_services.resolved == "g1"
    assert await services.build_child_graph_executor("GR", "gs1") == "child::GR::gs1"
    assert graph_services.built == ("GR", "gs1")


class _Session:
    id = "ses-1"
    turn_no = 7


def test_repark_continuation_builds_parked_state_and_request():
    pool = _bare_pool()
    parked = ParkedState(
        yielded=Yielded(tool_name="ask_user", event_key="ask_user:ses:old"),
        llm_messages=[{"role": "assistant", "parts": []}],
        turn_no=7,
        started_at=datetime.now(timezone.utc),
        tool_call_id="agent-tc",
    )
    new_frame = AgentFrame(
        "a", [], "tc-inner", 1, AgentResumeContext("ses", "ws", None, "u", []),
    )
    new_leaf = Yielded(
        tool_name="ask_user", event_key="ask_user:ses:new", timeout=120.0,
    )
    outcome = Repark(frames=[new_frame], leaf=new_leaf)

    out = pool._repark_continuation(_Session(), parked, outcome)

    assert out.success is True
    assert out.drop_lease is True
    assert out.park is not None
    assert out.park.parked_event_key == "ask_user:ses:new"

    rehydrated = ParkedState.from_jsonable(out.park.parked_state)
    # New leaf + new frames; session turn (llm_messages + tool_call_id) kept.
    assert rehydrated.yielded.event_key == "ask_user:ses:new"
    assert rehydrated.tool_call_id == "agent-tc"
    assert rehydrated.llm_messages == [{"role": "assistant", "parts": []}]
    assert len(rehydrated.frames) == 1
    assert rehydrated.frames[0].tool_call_id == "tc-inner"
