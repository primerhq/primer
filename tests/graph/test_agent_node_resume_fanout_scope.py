"""A parked fan-out agent instance re-renders its input_template on resume with ITS OWN scope.

The first dispatch (``_stream_node``) renders a fan-out instance's ``input_template`` against
``fanout_index`` and ``fanout_item``. ``_resume_agent_node`` renders it again, because the
resume rebuilds the node's user message from the template (it is the single source of that
message), but used to pass no scope at all: the template either failed on the undefined name
(StrictUndefined fails the node) or, where the name had a default, quietly gave the model a
different prompt than the one the instance started with.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.graph._node_refs import _FanoutInstance, _PendingAgentYield
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done, Message, StreamEvent, TextDelta, TextPart, ToolResultPart,
)
from primer.model.graph import (
    FanOutSpec,
    Graph,
    GraphContext,
    GraphNodeMessage,
    GraphThread,
    NodeOutput,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _StaticEdge,
)
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.model_profile import ModelProfileConfig
from primer.model.yield_ import Yielded, YieldToWorker
from primer.model_profile import ResolvedModel

from tests.graph.test_toolcall_dispatch import _InMemoryStorage
from tests.graph.test_workspace_executor import _make_state_repo


class _RecordingLLM:
    """Answers once and records the prompt it was handed."""

    def __init__(self) -> None:
        self.prompts: list[list[Message]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, **kw: Any) -> AsyncIterator[StreamEvent]:
        self.prompts.append(list(kw["messages"]))

        async def _g():
            yield TextDelta(text="continued", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
        return _g()


def _graph(template: str) -> Graph:
    return Graph(
        id="g-resume-fanout-scope",
        description="begin -> fan_out -> worker(agent) -> fan_in -> end",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="broadcast", target_node_id="worker", count=2)]),
            _AgentNodeRef(id="worker", agent_id="ag", input_template=template),
            _FanInNode(id="fi", aggregate_template="{{ nodes.worker | length }}"),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fo"),
            _StaticEdge(from_node="worker", to_node="fi"),
            _StaticEdge(from_node="fi", to_node="end"),
        ],
    )


async def _executor(template: str, llm: _RecordingLLM) -> GraphExecutor:
    graph = _graph(template)

    async def agent_resolver(agent_id: str) -> Agent:
        return Agent(id=agent_id, description="x", model=AgentModel(profile_id="p--m"), system_prompt=[])

    async def llm_resolver(agent: Agent):
        return (llm, ResolvedModel(
            profile_id="test-profile", provider_id="test-provider", model_name="m",
            context_length=128_000, config=ModelProfileConfig(),
        ))

    threads: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    messages: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=threads)  # type: ignore[arg-type]
    executor = GraphExecutor(
        graph=graph, agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=threads, message_storage=messages,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
    )
    executor._context = GraphContext(
        initial_input="seed", iteration=1,
        nodes={"begin": NodeOutput(text="seed", parsed=None, history=[], iteration=0)},
    )
    return executor


def _pending(node_id: str) -> _PendingAgentYield:
    return _PendingAgentYield(
        node_id=node_id, tool_call_id="tc-1", event_key="ask_user:gsid:tc-1", tool_name="ask_user",
        resume_metadata={"prompt": "continue?"},
        llm_messages=[Message(role="assistant", parts=[TextPart(text="(asking)")]).model_dump(mode="json")],
        iteration=1,
    )


def _answer() -> Message:
    return Message(role="tool", parts=[ToolResultPart(id="tc-1", output="yes")])


async def _resume(executor: GraphExecutor, node_id: str) -> None:
    async for _ev in executor._resume_agent_node(_pending(node_id), _answer(), {}):
        pass


def _user_texts(prompt: list[Message]) -> list[str]:
    return [p.text for m in prompt if m.role == "user" for p in m.parts if isinstance(p, TextPart)]


@pytest.mark.parametrize(
    ("node_id", "index", "item", "expected"),
    [("worker[0]", 0, "alpha", "item=alpha index=0"), ("worker[1]", 1, "beta", "item=beta index=1")],
)
async def test_a_resumed_instance_renders_its_template_with_its_own_fanout_scope(node_id, index, item, expected) -> None:
    llm = _RecordingLLM()
    executor = await _executor("item={{ fanout_item }} index={{ fanout_index }}", llm)
    executor._fanout_instances = {
        node_id: _FanoutInstance(synthesized_id=node_id, target_node_id="worker", fanout_index=index, fanout_item=item),
    }
    await _resume(executor, node_id)
    assert _user_texts(llm.prompts[-1]) == [expected]


async def test_a_node_output_item_is_usable_in_the_template_on_resume() -> None:
    """Broadcast and tee instances carry the FanOut's own NodeOutput as the item."""
    llm = _RecordingLLM()
    executor = await _executor("upstream said: {{ fanout_item.text }}", llm)
    executor._fanout_instances = {
        "worker[0]": _FanoutInstance(
            synthesized_id="worker[0]", target_node_id="worker", fanout_index=0,
            fanout_item=NodeOutput(text="the plan", parsed=None, history=[], iteration=0),
        ),
    }
    await _resume(executor, "worker[0]")
    assert _user_texts(llm.prompts[-1]) == ["upstream said: the plan"]


async def test_the_scope_survives_the_checkpoint_into_a_fresh_executor() -> None:
    """The real resume path: a NEW executor restores the checkpoint, then resumes."""
    first = await _executor("item={{ fanout_item.text }} index={{ fanout_index }}", _RecordingLLM())
    first._fanout_instances = {
        "worker[1]": _FanoutInstance(
            synthesized_id="worker[1]", target_node_id="worker", fanout_index=1,
            fanout_item=NodeOutput(text="beta plan", parsed=None, history=[], iteration=0),
        ),
    }
    checkpoint = first.snapshot_state()

    llm = _RecordingLLM()
    fresh = await _executor("item={{ fanout_item.text }} index={{ fanout_index }}", llm)
    fresh.restore_state(checkpoint)
    await _resume(fresh, "worker[1]")
    assert _user_texts(llm.prompts[-1]) == ["item=beta plan index=1"]


async def test_a_node_that_is_not_a_fanout_instance_still_renders_without_a_scope() -> None:
    llm = _RecordingLLM()
    executor = await _executor("plain input", llm)
    await _resume(executor, "worker")
    assert _user_texts(llm.prompts[-1]) == ["plain input"]


def test_the_first_dispatch_and_the_resume_build_the_scope_with_one_function() -> None:
    from primer.graph._node_refs import _fanout_scope

    instance = _FanoutInstance(synthesized_id="worker[2]", target_node_id="worker", fanout_index=2, fanout_item="gamma")
    assert _fanout_scope(instance) == {"fanout_index": 2, "fanout_item": "gamma"}
    assert _fanout_scope(None) is None
    tee = _FanoutInstance(synthesized_id="b", target_node_id="b", fanout_index=None, fanout_item="x")
    assert _fanout_scope(tee) == {"fanout_index": None, "fanout_item": "x"}


class _ParkInstanceOnce:
    """Stands in for ``run_agent_turn``. It records the user prompt of every call; the FIRST
    call whose prompt carries ``park_marker`` raises a bare ``YieldToWorker`` (as an ask_user
    or approval yield does), every other call answers."""

    def __init__(self, park_marker: str) -> None:
        self.park_marker = park_marker
        self.parked = False
        self.calls: list[tuple[str, bool]] = []  # (user text, was it the call that parked)

    async def __call__(self, *, prompt, messages_out, **_kwargs):
        user_text = _user_texts(prompt)[0]
        parks = self.park_marker in user_text and not self.parked
        self.calls.append((user_text, parks))
        if parks:
            self.parked = True
            raise YieldToWorker(
                Yielded(tool_name="ask_user", event_key="ask_user:t:tc1", resume_metadata={"prompt": "ok?"}),
                tool_call_id="tc1",
            )
        messages_out.append(Message(role="assistant", parts=[TextPart(text="done")]))
        yield TextDelta(text="done", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


async def test_a_parked_fanout_instance_resumes_with_the_prompt_it_started_with(tmp_path, monkeypatch) -> None:
    """End to end through real executors and the real checkpoint: worker[1] parks on its first
    dispatch, a fresh executor resumes it, and the prompt it is handed is the one it started
    with. Before the fix the resume failed the node: 'fanout_index' is undefined."""
    import primer.graph._agent_node as agent_node_mod

    graph = Graph(
        id="g-fanout-park", description="begin -> fo -> worker x2 -> fi -> end",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="broadcast", target_node_id="worker", count=2)]),
            _AgentNodeRef(id="worker", agent_id="ag", input_template="idx={{ fanout_index }}"),
            _FanInNode(id="fi", aggregate_template="{{ nodes.worker | length }}"),
            _EndNode(id="end", output_template="{{ nodes.fi.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fo"),
            _StaticEdge(from_node="worker", to_node="fi"),
            _StaticEdge(from_node="fi", to_node="end"),
        ],
    )
    turn = _ParkInstanceOnce(park_marker="idx=1")
    monkeypatch.setattr(agent_node_mod, "run_agent_turn", turn)

    async def build() -> WorkspaceGraphExecutor:
        repo = await _make_state_repo(tmp_path)

        async def agent_resolver(agent_id: str) -> Agent:
            return Agent(id=agent_id, description="x", model=AgentModel(profile_id="p--m"), system_prompt=[])

        async def llm_resolver(agent: Agent):
            return (object(), ResolvedModel(
                profile_id="test-profile", provider_id="test-provider", model_name="m",
                context_length=128_000, config=ModelProfileConfig(),
            ))

        return WorkspaceGraphExecutor(
            graph=graph, agent_resolver=agent_resolver,
            llm_resolver=llm_resolver,  # type: ignore[arg-type]
            state_repo=repo, graph_session_id="gsid-fanout-park",
        )

    first = await build()
    parked: YieldToWorker | None = None
    try:
        async for _ev in first.invoke([]):
            pass
    except YieldToWorker as exc:
        parked = exc
    assert parked is not None and parked.graph_checkpoint is not None, "worker[1] must park the graph"

    resumed = await build()
    answer = Message(role="tool", parts=[ToolResultPart(id="tc1", output="yes")])
    async for _ev in resumed.resume_from_checkpoint(parked.graph_checkpoint, resumed_tcid="tc1", agent_tool_result=answer):
        pass

    idx1 = [text for text, _parks in turn.calls if "idx=1" in text]
    assert idx1 == ["idx=1", "idx=1"], f"first dispatch and resume must carry the same user input, got {idx1}"
    assert not any("idx=1" not in text and "idx=0" not in text for text, _ in turn.calls)
    state = await resumed.load_state()
    assert state is not None and state["status"] == "ended" and state["ended_reason"] == "completed"
    assert state["node_states"]["worker[1]"]["status"] == "ended"
