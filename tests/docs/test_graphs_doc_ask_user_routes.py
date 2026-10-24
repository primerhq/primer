"""Workflow 7 of ``docs/agents/graphs.md`` routes on the person's answer (lead ruling 5 of ticket 01a16d48).

The workflow parks a graph on a ``system__ask_user`` ``tool_call`` node and routes on the reply. The node's text is ``{"response": ...}`` (the
``ask_user`` resume hook's result), and ``parsed`` is filled only when the node has an ``output_schema``. The page used to route on ``path: ""``
with no ``output_schema``, so ``parsed`` was null, no branch could match and every run, approved or not, took ``default_to``. This reads the
workflow's graph body from the page and runs it through the real park and resume path (``GraphExecutor`` raises the ``ask_user`` yield, the
resume runs the registered hook on the person's payload), with scripted agents.

``test_cookbook_graph_runs.py`` does not reach this body: it reads only documented ``create_graph`` / ``update_graph`` calls, and Workflow 7 shows
the bare ``{"entity": ...}`` arguments.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest

# Importing the system toolset registers the ``ask_user`` resume hook the value-yield resume runs.
import primer.toolset.system  # noqa: F401
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta
from primer.model.graph import Graph, GraphNodeMessage, GraphThread
from primer.model.model_profile import ModelProfileConfig
from primer.model.yield_ import Yielded, YieldTimeout, YieldToWorker
from primer.model_profile import ResolvedModel
from tests._resume_hook_fakes import drain, drain_until_yield
from tests.docs.test_docs_tool_call_bodies import REPO

GRAPHS_DOC = REPO / "docs" / "agents" / "graphs.md"


def _workflow_7() -> dict[str, Any]:
    text = GRAPHS_DOC.read_text(encoding="utf-8")
    section = text[text.index("### Workflow 7"):]
    section = section[: section.index("\n## ")]
    match = re.search(r"```json\n(?P<body>.*?)```", section, re.DOTALL)
    assert match is not None, "Workflow 7 has no json block"
    return json.loads(match["body"])["entity"]


class _Scripted:
    """An agent's model: always answers ``reply``."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def list_models(self):
        return ["m"]

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any):
        async def _events() -> AsyncIterator[StreamEvent]:
            yield TextDelta(text=self._reply, index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        return _events()


async def _run(fake_storage_provider, answer: Any) -> tuple[GraphThread, dict[str, Any], list[str]]:
    """Run Workflow 7 to its ``confirm`` park, answer it with ``answer`` (the resume payload) and drain; return the thread, the run's node
    outputs and the agents that ran."""
    graph = Graph.model_validate(_workflow_7())
    graph.assert_runnable()
    ran: list[str] = []

    async def agent_resolver(agent_id: str) -> Agent:
        return Agent(id=agent_id, description=agent_id, model=AgentModel(profile_id="p--m"), system_prompt=[])

    async def llm_resolver(agent: Agent):
        ran.append(agent.id)
        resolved = ResolvedModel(profile_id="p--m", provider_id="p", model_name="m", context_length=128_000, config=ModelProfileConfig())
        return _Scripted(f"{agent.id} done"), resolved

    async def ask(node, arguments):
        assert node.tool_id == "system__ask_user"
        raise YieldToWorker(
            Yielded(tool_name="ask_user", event_key="ask_user:sid:tc-confirm", resume_metadata={"prompt": arguments["prompt"]}),
            tool_call_id="tc-confirm",
        )

    async def never(node, arguments, bypass_approval=False):  # the resume answers the node, it does not call the tool again
        raise AssertionError("ask_user must not be dispatched again on resume")

    threads = fake_storage_provider.get_storage(GraphThread)
    messages = fake_storage_provider.get_storage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=threads)

    def executor(dispatcher) -> GraphExecutor:
        return GraphExecutor(
            graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver,  # type: ignore[arg-type]
            thread_storage=threads, message_storage=messages, graph_thread_id=thread.id, tool_dispatcher=dispatcher,
        )

    first = executor(ask)
    _, parked = await drain_until_yield(first.invoke({"change": "bump the API to v2"}))
    assert parked is not None, "the run must park on the confirm node"
    resumed = executor(never)
    await drain(resumed.resume_from_checkpoint(first.snapshot_state(), resumed_tcid="tc-confirm", toolcall_payload=answer))
    stored = await threads.get(thread.id)
    assert stored is not None
    return stored, dict(resumed._context.nodes if resumed._context else {}), ran


@pytest.mark.asyncio
async def test_an_approval_runs_the_deploy(fake_storage_provider) -> None:
    thread, nodes, ran = await _run(fake_storage_provider, {"response": "approve"})

    assert thread.ended_reason == "completed", (thread.ended_reason, thread.ended_detail)
    assert json.loads(nodes["confirm"].text) == {"response": "approve"}
    assert nodes["confirm"].parsed == {"response": "approve"}, "the output_schema is what fills parsed, which the router reads"
    assert ran == ["deploy-planner", "deployer"]
    assert "end_ok" in nodes and "end_no" not in nodes


@pytest.mark.asyncio
async def test_a_rejection_ends_on_the_rejected_branch(fake_storage_provider) -> None:
    thread, nodes, ran = await _run(fake_storage_provider, {"response": "reject"})

    assert thread.ended_reason == "completed"
    assert ran == ["deploy-planner"]
    assert "end_no" in nodes and "end_ok" not in nodes


@pytest.mark.asyncio
async def test_a_question_that_times_out_takes_the_rejected_branch_and_does_not_fail_the_run(fake_storage_provider) -> None:
    """A timeout answers ``{"timed_out": true, ...}``: it has no ``response``, so the schema (which does not require one) accepts it and the
    router takes ``default_to``."""
    thread, nodes, ran = await _run(fake_storage_provider, YieldTimeout(elapsed_seconds=60.0))

    assert thread.ended_reason == "completed", (thread.ended_reason, thread.ended_detail)
    assert nodes["confirm"].parsed is not None and nodes["confirm"].parsed.get("timed_out") is True
    assert ran == ["deploy-planner"]
    assert "end_no" in nodes and "end_ok" not in nodes
