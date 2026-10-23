"""Every model call of an agent turn and a compaction applies the profile's config.

A model profile carries ``config: {reasoning, extended}``. Nothing used to
apply it: ``run_agent_turn`` and both compaction summarisers called
``llm.stream`` with no ``extended`` (conflict C1). Each call now sends the
resolved profile's config, translated for the adapter's wire and the call's
model (:func:`primer.llm._reasoning.profile_extended`, sent through
:func:`primer.llm._reasoning.profile_stream`), and a call with no config
(``None``) or an empty one is made exactly as before: no ``extended`` keyword
at all.

The fake adapter below translates with Ollama's mapping, so "translated" is
observable: the profile says ``reasoning: off`` and the call carries
``think: False``. The graph agent-node call site is pinned in
``tests/graph/test_agent_node_profile_config.py``.
"""

from __future__ import annotations

import json
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
        self.translated_for: list[str | None] = []

    def profile_extended(
        self, config: ModelProfileConfig, *, model: str | None = None,
    ) -> dict[str, Any]:
        self.translated_for.append(model)
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
        # Translated once per turn, for the model the calls name.
        assert llm.translated_for == ["m"]

    @pytest.mark.parametrize("config", [None, ModelProfileConfig()])
    async def test_no_config_makes_the_call_exactly_as_before(self, config) -> None:
        llm = RecordingLLM([_text_round("ok")])
        await _turn(llm, config)
        assert "extended" not in llm.calls[0]

    async def test_the_adapter_kind_decides_the_wire_shape(self) -> None:
        """Same profile, another kind of adapter: its own translation."""
        from primer.llm._reasoning import anthropic_extended

        llm = RecordingLLM(
            [_text_round("ok")],
            translate=lambda c: anthropic_extended(c, model="claude-opus-4-7"),
        )
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
        assert llm.translated_for == ["m"]
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


class TestCompactionOnAClaudeModelThatRefusesSampling:
    """The real Anthropic adapter under compaction: compaction asks for
    temperature 0, which Claude Opus 4.7 and later, Sonnet 5 and later,
    Haiku 5.5 and Fable refuse on every request. The adapter drops it."""

    @pytest.mark.parametrize("model, kept", [
        ("claude-opus-4-7", False),
        ("claude-sonnet-5-5", False),
        ("claude-sonnet-4-6", True),
    ])
    @pytest.mark.parametrize("config", [None, ModelProfileConfig(reasoning=ReasoningLevel.OFF)])
    async def test_the_summariser_call_carries_no_refused_temperature(
        self, monkeypatch, model, kept, config,
    ) -> None:
        from pydantic import SecretStr

        from primer.llm.anthropic import AnthropicLLM
        from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType
        from tests.llm.test_anthropic import _aiter, _ok_events, _patched_client

        client = _patched_client(monkeypatch)
        client.messages.create.side_effect = lambda **_: _aiter(_ok_events())
        llm = AnthropicLLM(LLMProvider(
            id="ant-c", provider=LLMProviderType.ANTHROPIC,
            config=AnthropicConfig(api_key=SecretStr("sk-ant")),
            limits=Limits(max_concurrency=1),
        ))
        resolved = ResolvedModel(
            profile_id="p", provider_id="ant-c", model_name=model,
            context_length=1000, config=config,  # type: ignore[arg-type]
        )
        await CompactionStrategy(tail_turns=1).force_compact(
            agent=_agent(), llm=llm, model=resolved, history=list(HISTORY),
        )
        sent = client.messages.create.call_args.kwargs
        assert ("temperature" in sent) is kept


# ---- OpenAI reasoning models, captured at the HTTP transport (audit G2-02, G2-03) ----------------------------------------------

_SSE = (
    b'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"the summary"},"finish_reason":null}]}\n\n'
    b'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


@pytest.fixture
def openai_wire(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every JSON body the ``openchat`` adapter puts on the HTTP transport, whole."""
    from openai import AsyncOpenAI

    from tests.llm.test_openrouter import _sdk_http_module

    http = _sdk_http_module()
    bodies: list[dict] = []
    real = AsyncOpenAI

    def _handle(request):
        bodies.append(json.loads(request.content))
        return http.Response(200, content=_SSE, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(
        "primer.llm.openchat.AsyncOpenAI",
        lambda **kw: real(**kw, http_client=http.AsyncClient(transport=http.MockTransport(_handle))),
    )
    return bodies


def _openai_chat() -> LLM:
    from pydantic import HttpUrl, SecretStr

    from primer.llm.openchat import OpenChatLLM
    from primer.model.provider import Limits, LLMProvider, LLMProviderType, OpenChatConfig, OpenChatFlavor

    return OpenChatLLM(LLMProvider(
        id="openai-chat", provider=LLMProviderType.OPENCHAT,
        config=OpenChatConfig(url=HttpUrl("https://api.openai.com/v1/"), api_key=SecretStr("sk-test"), flavor=OpenChatFlavor.OPENAI),
        limits=Limits(max_concurrency=1),
    ))


def _on(model: str, config: ModelProfileConfig | None) -> ResolvedModel:
    return ResolvedModel(profile_id="p", provider_id="openai-chat", model_name=model, context_length=100_000, config=config)  # type: ignore[arg-type]


def _without_messages(body: dict) -> dict:
    """The whole body but the prompt (its text is the caller's; every other key is pinned)."""
    assert isinstance(body["messages"], list) and body["messages"]
    return {k: v for k, v in body.items() if k != "messages"}


_STREAM = {"stream": True, "stream_options": {"include_usage": True}}
_LOW = ModelProfileConfig(reasoning=ReasoningLevel.LOW)
# model, profile config -> what the summariser call carries beside the prompt
_COMPACTION_CASES = [
    ("o3", _LOW, {"max_completion_tokens": 4096, "reasoning_effort": "low"}),
    ("gpt-5", _LOW, {"max_completion_tokens": 4096, "reasoning_effort": "low"}),
    ("o3", None, {"max_completion_tokens": 4096}),
    ("gpt-5", ModelProfileConfig(), {"max_completion_tokens": 4096}),
    ("o4-mini", None, {"max_completion_tokens": 4096}),
    ("gpt-4o", _LOW, {"max_tokens": 4096, "temperature": 0.0}),
    ("my-deployment", None, {"max_tokens": 4096, "temperature": 0.0}),
]


class TestOpenAIReasoningModelsOnTheWire:
    """OpenAI's reasoning models refuse ``max_tokens`` (they take ``max_completion_tokens``) and ``temperature`` / ``top_p``
    unless the effort is ``none``, whether or not an effort is sent: they reason by default. Compaction asks for
    ``temperature: 0.0`` and a 4096 cap, an agent turn sends the agent's temperature and output cap; on a reasoning model each
    call carries the shape OpenAI accepts. A model that does not reason (gpt-4o) and one the table does not know are unchanged."""

    @pytest.mark.parametrize("model, config, expected", _COMPACTION_CASES)
    async def test_the_compaction_summariser_call(self, openai_wire, model, config, expected) -> None:
        assert CompactionStrategy.DEFAULT_SUMMARY_MAX_TOKENS == 4096
        await CompactionStrategy(tail_turns=1).force_compact(agent=_agent(), llm=_openai_chat(), model=_on(model, config), history=list(HISTORY))
        assert len(openai_wire) == 1
        assert _without_messages(openai_wire[0]) == {"model": model, **_STREAM, **expected}

    @pytest.mark.parametrize("model, config, expected", [
        ("o3", _LOW, {"max_completion_tokens": 2048, "reasoning_effort": "low"}),
        ("gpt-5", None, {"max_completion_tokens": 2048}),
        ("o4-mini", None, {"max_completion_tokens": 2048}),
        ("gpt-4o", None, {"max_tokens": 2048, "temperature": 0.7}),
        ("my-deployment", None, {"max_tokens": 2048, "temperature": 0.7}),
    ])
    async def test_an_agent_turn_call(self, openai_wire, model, config, expected) -> None:
        agent = AGENT.model_copy(update={"temperature": 0.7, "max_output_tokens": 2048})
        async for _ in run_agent_turn(
            agent=agent, llm=_openai_chat(), llm_model=_on(model, config),
            tool_manager=_Manager(), prompt=list(_USER),  # type: ignore[arg-type]
        ):
            pass
        assert len(openai_wire) == 1
        tools = [{"type": "function", "function": {"name": "loop_tool", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
        assert _without_messages(openai_wire[0]) == {"model": model, **_STREAM, "tools": tools, "tool_choice": "auto", **expected}
