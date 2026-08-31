"""llm.called at the shared model-call seam (run_agent_turn).

Mirrors tests/observability/test_llm_profile_instrumentation.py's
harness: same seam, so every executor's calls are counted once -
with a recorder-wired manager they now also land on the event log.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.agent.loop import run_agent_turn
from primer.agent.tool_manager import ToolExecutionManager
from primer.events.recorder import EventRecorder
from primer.model.agent import Agent
from primer.model.chat import Done, Message, TextDelta, TextPart, Usage
from primer.model.model_profile import ModelProfileConfig
from primer.model.provider import SqliteConfig
from primer.model_profile import ResolvedModel
from primer.storage.sqlite import SqliteStorageProvider

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


class _FakeLLM:
    def __init__(self, rounds):
        self._rounds = list(rounds)

    def stream(self, **_kwargs):
        events = self._rounds.pop(0)

        async def _gen():
            for ev in events:
                yield ev

        return _gen()


class _BoomLLM:
    def stream(self, **_kwargs):
        async def _gen():
            yield TextDelta(text="par", index=0)
            raise RuntimeError("provider fell over")

        return _gen()


def _model() -> ResolvedModel:
    return ResolvedModel(
        profile_id="prof-1",
        provider_id="prov-1",
        model_name="m-1",
        context_length=1000,
        config=ModelProfileConfig(),
    )


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


def _manager(sp=None) -> ToolExecutionManager:
    recorder = EventRecorder(sp.get_event_store()) if sp else None
    return ToolExecutionManager(
        toolset_providers={}, tools=[], event_recorder=recorder,
    )


async def _drain(llm, manager) -> list:
    out = []
    async for ev in run_agent_turn(
        agent=Agent(id="ag-1", description="d",
                    model={"profile_id": "prof-1"}),
        llm=llm,
        llm_model=_model(),
        tool_manager=manager,
        prompt=[Message(role="user", parts=[TextPart(text="hi")])],
    ):
        out.append(ev)
    return out


async def test_ok_call_lands_metered_event(sp):
    await _drain(_FakeLLM([[
        TextDelta(text="hello", index=0),
        Usage(input_tokens=11, output_tokens=7, cumulative=False),
        Done(stop_reason="stop", raw_reason="stop"),
    ]]), _manager(sp))

    [event] = await sp.get_event_store().read_after(0)
    assert event.event_type == "llm.called"
    assert event.payload["profile_id"] == "prof-1"
    assert event.payload["provider_id"] == "prov-1"
    assert event.payload["input_tokens"] == 11
    assert event.payload["output_tokens"] == 7
    assert event.payload["status"] == "ok"
    assert event.payload["duration_ms"] >= 0


async def test_error_call_lands_error_event(sp):
    with pytest.raises(RuntimeError, match="provider fell over"):
        await _drain(_BoomLLM(), _manager(sp))
    [event] = await sp.get_event_store().read_after(0)
    assert event.event_type == "llm.called"
    assert event.payload["status"] == "error"


async def test_recorderless_manager_emits_nothing(sp):
    await _drain(_FakeLLM([[
        Done(stop_reason="stop", raw_reason="stop"),
    ]]), _manager(None))
    assert await sp.get_event_store().read_after(0) == []
