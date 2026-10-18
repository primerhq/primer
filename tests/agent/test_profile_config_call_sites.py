"""Every model call of an agent turn and a compaction applies the profile's config.

A model profile carries ``config: {reasoning, extended}``. Nothing used to
apply it: ``run_agent_turn`` and both compaction summarisers called
``llm.stream`` with no ``extended`` (conflict C1). Each call now sends the
resolved profile's config, translated for the adapter's wire
(:func:`primer.llm._reasoning.profile_stream_kwargs`), and a call with no
config (``None``) or an empty one is made exactly as before: no ``extended``
keyword at all.

The fake adapter below translates with Ollama's mapping, so "translated" is
observable: the profile says ``reasoning: off`` and the call carries
``think: False``. The graph agent-node call site is pinned in
``tests/graph/test_agent_node_profile_config.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.agent.loop import run_agent_turn
from primer.int.llm import LLM
from primer.llm._reasoning import ollama_extended
from primer.model.chat import Message, StreamEvent, TextPart
from primer.model.model_profile import ModelProfileConfig, ReasoningLevel
from primer.model_profile import ResolvedModel
from tests.agent.test_compaction_tools import (
    HISTORY,
    _FakeToolManager,
    _agent,
    _text_round,
    _tool_round,
)
from tests.agent.test_run_agent_turn_seam import AGENT, _Manager

# reasoning "off" plus a passthrough knob ...
CONFIG = ModelProfileConfig(reasoning=ReasoningLevel.OFF, extended={"seed": 3})
# ... as Ollama's wire takes it.
TRANSLATED = {"seed": 3, "think": False}


class RecordingLLM(LLM):
    """A fake adapter of one provider kind.

    It translates a profile's config with that kind's own mapping (Ollama's
    by default) and records the keyword arguments of every ``stream`` call
    except the prompt itself.
    """

    def __init__(
        self,
        rounds: list[list[StreamEvent]],
        *,
        translate: Callable[[ModelProfileConfig], dict[str, Any]] = ollama_extended,
    ) -> None:
        self._rounds = list(rounds)
        self._translate = translate
        self.calls: list[dict[str, Any]] = []

    def profile_extended(self, config: ModelProfileConfig) -> dict[str, Any]:
        return self._translate(config)

    def stream(self, *, model, messages, **kwargs):  # type: ignore[override]
        self.calls.append({"model": model, **kwargs})
        script = self._rounds.pop(0) if self._rounds else _text_round("done")

        async def _gen() -> AsyncIterator[StreamEvent]:
            for ev in script:
                yield ev

        return _gen()

    async def count_tokens(self, *, model, messages, tools=None) -> int:
        return 1


def _resolved(config: ModelProfileConfig | None) -> ResolvedModel:
    return ResolvedModel(
        profile_id="p", provider_id="prov", model_name="m",
        context_length=1000, config=config,  # type: ignore[arg-type]
    )


_USER = [Message(role="user", parts=[TextPart(text="go")])]


async def _turn(llm: RecordingLLM, config: ModelProfileConfig | None) -> None:
    async for _ in run_agent_turn(
        agent=AGENT, llm=llm, llm_model=_resolved(config),
        tool_manager=_Manager(), prompt=list(_USER),  # type: ignore[arg-type]
    ):
        pass


class TestAgentTurn:
    async def test_every_call_of_a_turn_carries_the_translated_config(self) -> None:
        """The first call and the one after a tool round both apply it."""
        llm = RecordingLLM([_tool_round("c1", "loop_tool", {}), _text_round("ok")])
        await _turn(llm, CONFIG)
        assert len(llm.calls) == 2
        assert [c["extended"] for c in llm.calls] == [TRANSLATED, TRANSLATED]

    @pytest.mark.parametrize("config", [None, ModelProfileConfig()])
    async def test_no_config_makes_the_call_exactly_as_before(self, config) -> None:
        llm = RecordingLLM([_text_round("ok")])
        await _turn(llm, config)
        assert "extended" not in llm.calls[0]

    async def test_the_adapter_kind_decides_the_wire_shape(self) -> None:
        """Same profile, another kind of adapter: its own translation."""
        from primer.llm._reasoning import anthropic_extended

        llm = RecordingLLM([_text_round("ok")], translate=anthropic_extended)
        await _turn(llm, CONFIG)
        assert llm.calls[0]["extended"] == {"seed": 3, "thinking": {"type": "disabled"}}


async def _compact(llm: RecordingLLM, config: ModelProfileConfig | None, *, tools: bool) -> None:
    async def sink(_ev: StreamEvent) -> None:
        return None

    kwargs: dict[str, Any] = {"tool_manager": _FakeToolManager(), "event_sink": sink} if tools else {}
    await CompactionStrategy(tail_turns=1).force_compact(
        agent=_agent(), llm=llm, model=_resolved(config), history=list(HISTORY), **kwargs,
    )


class TestCompaction:
    async def test_a_text_only_summariser_call_carries_the_translated_config(self) -> None:
        llm = RecordingLLM([_text_round("summary")])
        await _compact(llm, CONFIG, tools=False)
        assert len(llm.calls) == 1
        assert llm.calls[0]["extended"] == TRANSLATED
        # The summariser's own settings are untouched by the profile's.
        assert llm.calls[0]["temperature"] == 0.0

    async def test_every_call_of_a_tool_using_summariser_carries_it(self) -> None:
        llm = RecordingLLM([
            _tool_round("c1", "workspace__write", {"path": "dump.md"}),
            _text_round("summary"),
        ])
        await _compact(llm, CONFIG, tools=True)
        assert len(llm.calls) == 2
        assert [c["extended"] for c in llm.calls] == [TRANSLATED, TRANSLATED]

    @pytest.mark.parametrize("tools", [False, True])
    @pytest.mark.parametrize("config", [None, ModelProfileConfig()])
    async def test_no_config_makes_the_calls_exactly_as_before(self, config, tools) -> None:
        llm = RecordingLLM([_text_round("summary")])
        await _compact(llm, config, tools=tools)
        assert llm.calls and all("extended" not in c for c in llm.calls)
