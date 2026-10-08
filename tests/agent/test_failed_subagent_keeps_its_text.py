"""Through the real ``run_subagent``: a subagent that streams text and then fails, or raises, keeps its text under its own run (ticket 01a11ca9).

Drives ``run_subagent`` with the recorder the dispatch publishes, the way ``tests/agent/test_delegated_runs_carry_a_run_id.py`` does. The recorder-level
cases are in ``tests/session/test_delegation_recorder_failed_run.py``; these pin that the invoke loop actually calls ``finish_run`` when a run ends by an
exception, which no event can tell the recorder.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.agent.invoke import invocation_depth_guard, run_subagent
from primer.model.chat import Done, Error, StreamStart, TextDelta
from primer.session.delegation import DelegationRecorder, reset_delegation_sink, set_delegation_sink
from tests.agent.test_delegated_runs_carry_a_run_id import _Bus, _ProviderRegistry, _StorageProvider, _Writer, _agent, _provider_row


class _Script:
    """One stream per ``stream`` call; an entry that is an exception CLASS is raised at that point of the stream."""

    def __init__(self, scripts: list[list]) -> None:
        self._scripts = list(scripts)

    def stream(self, *, model, messages, **kwargs):  # noqa: ANN001
        script = self._scripts.pop(0)

        async def _gen() -> AsyncIterator:
            for ev in script:
                if isinstance(ev, type) and issubclass(ev, BaseException):
                    raise ev("the stream broke")
                yield ev

        return _gen()


def _world(scripts: list[list]):
    storage = _StorageProvider(agent=_agent(tools=[]), provider_row=_provider_row())
    registry = _ProviderRegistry(llm=_Script(scripts), toolset=None)
    return storage, registry


async def _run(storage, registry, writer: _Writer, prompt: str = "go") -> str:
    with invocation_depth_guard():
        return await run_subagent(
            agent_id="agent-sub", prompt=prompt, storage_provider=storage, provider_registry=registry,
            principal="user-1", session_id="sess-1", workspace_id="ws-1", invoke_tool_call_id="call_0", turn_no=1,
        )


async def _recording(coro_factory) -> _Writer:
    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
    try:
        await coro_factory(writer)
    finally:
        reset_delegation_sink(token)
    return writer


def _tokens(writer: _Writer) -> list[tuple[str, str]]:
    return [(r.payload["text"], r.payload["delegate_run_id"]) for r in writer.records if r.kind.value == "assistant_token"]


async def test_a_subagent_that_streams_text_and_then_fails_keeps_the_text_as_its_own_record() -> None:
    storage, registry = _world([
        [StreamStart(model="m1"), TextDelta(index=0, text="partial answer"), Error(message="the model fell over", code="server_error", fatal=True)],
        [StreamStart(model="m1"), TextDelta(index=0, text="second run"), Done(stop_reason="stop", raw_reason="stop")],
    ])

    async def work(writer: _Writer) -> None:
        with pytest.raises(Exception):  # noqa: B017,PT011 - the failed stream ends the run with whatever the loop raises for it
            await _run(storage, registry, writer)
        await _run(storage, registry, writer, prompt="again")

    writer = await _recording(work)
    tokens = _tokens(writer)
    assert [text for text, _run_id in tokens] == ["partial answer", "second run"], tokens
    assert tokens[0][1] != tokens[1][1], "the two runs have their own ids"
    kinds = [r.kind.value for r in writer.records]
    assert kinds.index("assistant_token") < kinds.index("error"), "the text is ahead of the error"


async def test_a_subagent_whose_stream_raises_after_streaming_text_keeps_the_text() -> None:
    storage, registry = _world([
        [StreamStart(model="m1"), TextDelta(index=0, text="half an answer"), RuntimeError],
        [StreamStart(model="m1"), TextDelta(index=0, text="second run"), Done(stop_reason="stop", raw_reason="stop")],
    ])

    async def work(writer: _Writer) -> None:
        with pytest.raises(Exception):  # noqa: B017,PT011
            await _run(storage, registry, writer)
        await _run(storage, registry, writer, prompt="again")

    writer = await _recording(work)
    assert [text for text, _run_id in _tokens(writer)] == ["half an answer", "second run"], _tokens(writer)
