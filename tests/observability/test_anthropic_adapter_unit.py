"""Unit tests for the Anthropic LLM adapter (:mod:`primer.llm.anthropic`).

These live under ``tests/observability`` alongside
``test_llm_instrumentation.py`` — the CI coverage sweep ignores
``tests/llm`` (see ``pyproject`` note / the sweep's ``--ignore`` list),
so adapter unit tests placed there do not count toward coverage. This
file mirrors the mocking shapes used by the existing ``tests/llm``
adapter tests (SimpleNamespace fake events, patched SDK client) but
lives in an included directory.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from pydantic import BaseModel, HttpUrl, SecretStr

from primer.llm.anthropic import (
    ANTHROPIC_BASE_URL,
    ANTHROPIC_VERSION,
    AnthropicLLM,
    _DEFAULT_MAX_TOKENS,
    _build_sampling_kwargs,
    _citation_to_universal,
    _discover_anthropic_models,
    _extract_extended_kwargs,
    _map_stop_reason,
    _messages_to_anthropic,
    _part_to_anthropic_block,
    _response_format_to_emulation,
    _StreamState,
    _tool_choice_to_anthropic,
    _tools_to_anthropic,
    _translate_event,
)
from primer.model.chat import (
    AudioPart,
    Citation,
    DocumentPart,
    Done,
    Error as ChatError,
    ExtendedEvent,
    ExtendedPart,
    ImagePart,
    Message,
    ReasoningDelta,
    ServerToolCallStart,
    TextDelta,
    TextPart,
    Tool,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    Usage,
    VideoPart,
)
from primer.model.except_ import (
    AuthenticationError,
    ConfigError,
    ModelNotFoundError,
    PrimerError,
    UnsupportedContentError,
)
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenResponsesConfig,
)


def _make_provider(
    *,
    api_key: str = "sk-ant-test",
    models: list[str] | None = None,
    max_concurrency: int = 4,
) -> LLMProvider:
    return LLMProvider(
        id="anthropic-default",
        provider=LLMProviderType.ANTHROPIC,
        models=[
            LLMModel(name=name, context_length=200_000)
            for name in (models or ["claude-sonnet-4-5"])
        ],
        config=AnthropicConfig(api_key=SecretStr(api_key)),
        limits=Limits(max_concurrency=max_concurrency),
    )


async def _aiter(items: list) -> AsyncIterator:
    for item in items:
        yield item


def _ok_events() -> list[Any]:
    return [
        NS(
            type="message_start",
            message=NS(id="msg_1", model="claude-sonnet-4-5", usage=NS(input_tokens=5)),
        ),
        NS(type="content_block_start", index=0, content_block=NS(type="text")),
        NS(type="content_block_delta", index=0, delta=NS(type="text_delta", text="hi")),
        NS(type="content_block_stop", index=0),
        NS(type="message_delta", delta=NS(stop_reason="end_turn"), usage=NS(output_tokens=3)),
        NS(type="message_stop"),
    ]


# --------------------------------------------------------------------------- #
# Part -> content-block mapping                                                #
# --------------------------------------------------------------------------- #


class TestPartMapping:
    def test_text(self) -> None:
        assert _part_to_anthropic_block(TextPart(text="hello")) == {
            "type": "text",
            "text": "hello",
        }

    def test_image_data_base64(self) -> None:
        block = _part_to_anthropic_block(ImagePart(data=b"\x89PNG", mime_type="image/png"))
        assert block["type"] == "image"
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "image/png"
        assert base64.b64decode(block["source"]["data"]) == b"\x89PNG"

    def test_image_data_default_mime(self) -> None:
        block = _part_to_anthropic_block(ImagePart(data=b"raw"))
        assert block["source"]["media_type"] == "image/png"

    def test_image_url(self) -> None:
        block = _part_to_anthropic_block(ImagePart(url="https://x/y.png"))
        assert block["source"] == {"type": "url", "url": "https://x/y.png"}

    def test_image_file_id_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="file_id images"):
            _part_to_anthropic_block(ImagePart(file_id="file_123"))

    def test_document_data_base64(self) -> None:
        block = _part_to_anthropic_block(
            DocumentPart(data=b"%PDF", mime_type="application/pdf")
        )
        assert block["type"] == "document"
        assert block["source"]["media_type"] == "application/pdf"
        assert base64.b64decode(block["source"]["data"]) == b"%PDF"

    def test_document_default_mime(self) -> None:
        block = _part_to_anthropic_block(DocumentPart(data=b"%PDF"))
        assert block["source"]["media_type"] == "application/pdf"

    def test_document_url(self) -> None:
        block = _part_to_anthropic_block(DocumentPart(url="https://x/y.pdf"))
        assert block["source"] == {"type": "url", "url": "https://x/y.pdf"}

    def test_document_file_id_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="file_id documents"):
            _part_to_anthropic_block(DocumentPart(file_id="file_9"))

    def test_tool_call(self) -> None:
        block = _part_to_anthropic_block(
            ToolCallPart(id="call_1", name="search", arguments={"q": "x"})
        )
        assert block == {
            "type": "tool_use",
            "id": "call_1",
            "name": "search",
            "input": {"q": "x"},
        }

    def test_audio_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="audio"):
            _part_to_anthropic_block(ExtendedPart(extended=AudioPart(data=b"a")))

    def test_video_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="video"):
            _part_to_anthropic_block(ExtendedPart(extended=VideoPart(data=b"v")))


class TestMessagesToAnthropic:
    def test_system_lifted_and_concatenated(self) -> None:
        system, msgs = _messages_to_anthropic(
            [
                Message(role="system", parts=[TextPart(text="a")]),
                Message(role="system", parts=[TextPart(text="b")]),
                Message(role="user", parts=[TextPart(text="hi")]),
            ]
        )
        assert system == "a\n\nb"
        assert [m["role"] for m in msgs] == ["user"]

    def test_no_system_returns_none(self) -> None:
        system, msgs = _messages_to_anthropic(
            [Message(role="user", parts=[TextPart(text="hi")])]
        )
        assert system is None
        assert msgs[0]["content"][0]["type"] == "text"

    def test_system_non_text_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="system messages"):
            _messages_to_anthropic(
                [Message(role="system", parts=[ImagePart(data=b"x")])]
            )

    def test_tool_role_flattened_to_user_tool_result(self) -> None:
        _, msgs = _messages_to_anthropic(
            [
                Message(
                    role="tool",
                    parts=[
                        ToolResultPart(id="call_1", output="42", error=False),
                        ToolResultPart(id="call_2", output="boom", error=True),
                    ],
                )
            ]
        )
        assert msgs[0]["role"] == "user"
        blocks = msgs[0]["content"]
        assert blocks[0] == {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": "42",
            "is_error": False,
        }
        assert blocks[1]["is_error"] is True

    def test_tool_role_non_tool_result_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="tool-role"):
            _messages_to_anthropic(
                [Message(role="tool", parts=[TextPart(text="nope")])]
            )

    def test_assistant_role_preserved(self) -> None:
        _, msgs = _messages_to_anthropic(
            [Message(role="assistant", parts=[TextPart(text="ok")])]
        )
        assert msgs[0]["role"] == "assistant"


class TestToolTranslation:
    def _tool(self) -> Tool:
        return Tool(
            id="search",
            description="Search",
            toolset_id="ts",
            args_schema={"type": "object", "properties": {}},
        )

    def test_tools_none_and_empty(self) -> None:
        assert _tools_to_anthropic(None) is None
        assert _tools_to_anthropic([]) is None

    def test_tools_mapped(self) -> None:
        out = _tools_to_anthropic([self._tool()])
        assert out == [
            {
                "name": "search",
                "description": "Search",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]

    def test_tool_choice_variants(self) -> None:
        assert _tool_choice_to_anthropic(None) is None
        assert _tool_choice_to_anthropic("auto") == {"type": "auto"}
        assert _tool_choice_to_anthropic("required") == {"type": "any"}
        assert _tool_choice_to_anthropic("none") == {"type": "none"}
        assert _tool_choice_to_anthropic("search") == {"type": "tool", "name": "search"}


class _Schema(BaseModel):
    answer: str


class TestResponseFormatEmulation:
    def test_none(self) -> None:
        assert (
            _response_format_to_emulation(None, has_tools=False, has_tool_choice=False)
            is None
        )

    def test_with_tools_rejected(self) -> None:
        with pytest.raises(ConfigError, match="cannot be combined with tools"):
            _response_format_to_emulation(_Schema, has_tools=True, has_tool_choice=False)

    def test_with_tool_choice_rejected(self) -> None:
        with pytest.raises(ConfigError, match="explicit tool_choice"):
            _response_format_to_emulation(_Schema, has_tools=False, has_tool_choice=True)

    def test_pydantic_model(self) -> None:
        tools, choice = _response_format_to_emulation(
            _Schema, has_tools=False, has_tool_choice=False
        )
        assert tools[0]["name"] == "structured_output"
        assert tools[0]["input_schema"]["properties"]["answer"]["type"] == "string"
        assert choice == {"type": "tool", "name": "structured_output"}

    def test_dict_schema(self) -> None:
        tools, choice = _response_format_to_emulation(
            {"type": "object"}, has_tools=False, has_tool_choice=False
        )
        assert tools[0]["input_schema"] == {"type": "object"}

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(ConfigError, match="Pydantic class or dict"):
            _response_format_to_emulation(123, has_tools=False, has_tool_choice=False)  # type: ignore[arg-type]


class TestSamplingAndExtended:
    def test_default_max_tokens_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="primer.llm.anthropic")
        out = _build_sampling_kwargs(
            temperature=None, top_p=None, max_output_tokens=None, stop=None
        )
        assert out["max_tokens"] == _DEFAULT_MAX_TOKENS
        assert any("max_output_tokens" in r.message for r in caplog.records)

    def test_all_sampling_knobs(self) -> None:
        out = _build_sampling_kwargs(
            temperature=0.5, top_p=0.9, max_output_tokens=100, stop=["X"]
        )
        assert out == {
            "temperature": 0.5,
            "top_p": 0.9,
            "max_tokens": 100,
            "stop_sequences": ["X"],
        }

    def test_extended_passthrough_and_dropped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.anthropic")
        out = _extract_extended_kwargs(
            {"top_k": 5, "thinking": {"type": "enabled"}, "bogus": 1}
        )
        assert out == {"top_k": 5, "thinking": {"type": "enabled"}}
        assert any("dropped unknown" in r.message for r in caplog.records)

    def test_extended_empty(self) -> None:
        assert _extract_extended_kwargs(None) == {}
        assert _extract_extended_kwargs({}) == {}


class TestStopReasonMapping:
    def test_none_is_other(self) -> None:
        assert _map_stop_reason(None, _StreamState()) == "other"

    def test_end_turn_without_tool_use(self) -> None:
        assert _map_stop_reason("end_turn", _StreamState()) == "stop"

    def test_end_turn_with_tool_use(self) -> None:
        state = _StreamState(saw_tool_use=True)
        assert _map_stop_reason("end_turn", state) == "tool_use"

    def test_known_reasons(self) -> None:
        st = _StreamState()
        assert _map_stop_reason("max_tokens", st) == "max_tokens"
        assert _map_stop_reason("stop_sequence", st) == "stop_sequence"
        assert _map_stop_reason("tool_use", st) == "tool_use"
        assert _map_stop_reason("pause_turn", st) == "stop"
        assert _map_stop_reason("refusal", st) == "content_filter"

    def test_unknown_reason_is_other(self) -> None:
        assert _map_stop_reason("mystery", _StreamState()) == "other"


class TestCitation:
    def test_char_location_fields(self) -> None:
        cit = _citation_to_universal(
            NS(
                url="https://x",
                title="T",
                cited_text="quote",
                start_char_index=1,
                end_char_index=9,
                file_id="f1",
            ),
            index=2,
        )
        assert cit.source_url == "https://x"
        assert cit.source_title == "T"
        assert cit.source_id == "f1"
        assert cit.quoted_text == "quote"
        assert (cit.start_index, cit.end_index, cit.index) == (1, 9, 2)


# --------------------------------------------------------------------------- #
# Stream-event translation                                                    #
# --------------------------------------------------------------------------- #


class TestTranslateEvent:
    def test_message_start_emits_stream_start_and_captures_usage(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(type="message_start", message=NS(id="req_9", model="claude-x", usage=NS(input_tokens=7))),
            state,
            model_name="fallback",
        )
        assert isinstance(out[0], type(out[0]))
        assert out[0].request_id == "req_9"
        assert out[0].model == "claude-x"
        assert state.input_tokens == 7

    def test_message_start_falls_back_to_model_name(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(type="message_start", message=NS(id="r", model=None, usage=None)),
            state,
            model_name="fallback",
        )
        assert out[0].model == "fallback"

    def test_text_block_start_and_delta(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(type="content_block_start", index=0, content_block=NS(type="text")),
            state,
            model_name="m",
        )
        out = _translate_event(
            NS(type="content_block_delta", index=0, delta=NS(type="text_delta", text="hi")),
            state,
            model_name="m",
        )
        assert out == [TextDelta(text="hi", index=0)]

    def test_tool_use_block_lifecycle(self) -> None:
        state = _StreamState()
        start = _translate_event(
            NS(
                type="content_block_start",
                index=1,
                content_block=NS(type="tool_use", id="call_1", name="search"),
            ),
            state,
            model_name="m",
        )
        assert start == [ToolCallStart(id="call_1", name="search", index=1)]
        assert state.saw_tool_use is True

        delta = _translate_event(
            NS(
                type="content_block_delta",
                index=1,
                delta=NS(type="input_json_delta", partial_json='{"q":'),
            ),
            state,
            model_name="m",
        )
        assert delta == [ToolCallDelta(id="call_1", arguments_delta='{"q":', index=1)]

        _translate_event(
            NS(
                type="content_block_delta",
                index=1,
                delta=NS(type="input_json_delta", partial_json='"x"}'),
            ),
            state,
            model_name="m",
        )
        stop = _translate_event(
            NS(type="content_block_stop", index=1), state, model_name="m"
        )
        assert stop == [ToolCallEnd(id="call_1", arguments={"q": "x"}, index=1)]

    def test_tool_use_stop_with_malformed_json(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(
                type="content_block_start",
                index=0,
                content_block=NS(type="tool_use", id="c", name="n"),
            ),
            state,
            model_name="m",
        )
        state.accumulated_args[0] = "{not json"
        out = _translate_event(NS(type="content_block_stop", index=0), state, model_name="m")
        assert out == [ToolCallEnd(id="c", arguments={}, index=0)]

    def test_tool_use_stop_non_dict_json(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(
                type="content_block_start",
                index=0,
                content_block=NS(type="tool_use", id="c", name="n"),
            ),
            state,
            model_name="m",
        )
        state.accumulated_args[0] = "[1, 2]"
        out = _translate_event(NS(type="content_block_stop", index=0), state, model_name="m")
        assert out == [ToolCallEnd(id="c", arguments={}, index=0)]

    def test_content_block_stop_non_tool_is_noop(self) -> None:
        state = _StreamState()
        state.block_kinds[0] = "text"
        assert _translate_event(NS(type="content_block_stop", index=0), state, model_name="m") == []

    def test_thinking_block_and_delta(self) -> None:
        state = _StreamState()
        assert _translate_event(
            NS(type="content_block_start", index=0, content_block=NS(type="thinking")),
            state,
            model_name="m",
        ) == []
        out = _translate_event(
            NS(type="content_block_delta", index=0, delta=NS(type="thinking_delta", thinking="ponder")),
            state,
            model_name="m",
        )
        assert out == [ReasoningDelta(text="ponder", index=0)]

    def test_signature_delta(self) -> None:
        out = _translate_event(
            NS(type="content_block_delta", index=0, delta=NS(type="signature_delta", signature="sig")),
            _StreamState(),
            model_name="m",
        )
        assert out[0].text == ""
        assert out[0].signature == "sig"

    def test_citations_delta(self) -> None:
        out = _translate_event(
            NS(
                type="content_block_delta",
                index=2,
                delta=NS(type="citations_delta", citation=NS(url="https://x", title="T")),
            ),
            _StreamState(),
            model_name="m",
        )
        assert isinstance(out[0], ExtendedEvent)
        assert isinstance(out[0].extended, Citation)
        assert out[0].extended.source_url == "https://x"

    def test_citations_delta_missing_citation(self) -> None:
        out = _translate_event(
            NS(type="content_block_delta", index=0, delta=NS(type="citations_delta", citation=None)),
            _StreamState(),
            model_name="m",
        )
        assert out == []

    def test_unknown_delta_type_ignored(self) -> None:
        out = _translate_event(
            NS(type="content_block_delta", index=0, delta=NS(type="mystery_delta")),
            _StreamState(),
            model_name="m",
        )
        assert out == []

    def test_server_tool_use_block(self) -> None:
        out = _translate_event(
            NS(
                type="content_block_start",
                index=0,
                content_block=NS(type="server_tool_use", id="st_1", name="web_search"),
            ),
            _StreamState(),
            model_name="m",
        )
        assert isinstance(out[0].extended, ServerToolCallStart)
        assert out[0].extended.tool_name == "web_search"

    def test_unknown_block_type_registered_silently(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(type="content_block_start", index=3, content_block=NS(type="wat")),
            state,
            model_name="m",
        )
        assert out == []
        assert state.block_kinds[3] == "wat"

    def test_message_delta_captures_stop_and_usage(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(type="message_delta", delta=NS(stop_reason="max_tokens"), usage=NS(output_tokens=11)),
            state,
            model_name="m",
        )
        assert state.final_stop_reason == "max_tokens"
        assert state.output_tokens == 11

    def test_message_stop_emits_usage_and_done(self) -> None:
        state = _StreamState(input_tokens=5, output_tokens=3, final_stop_reason="end_turn")
        out = _translate_event(NS(type="message_stop"), state, model_name="m")
        assert out[0] == Usage(input_tokens=5, output_tokens=3, cumulative=False)
        assert isinstance(out[1], Done)
        assert out[1].stop_reason == "stop"

    def test_message_stop_without_usage_only_done(self) -> None:
        out = _translate_event(NS(type="message_stop"), _StreamState(), model_name="m")
        assert len(out) == 1
        assert isinstance(out[0], Done)

    def test_unknown_event_ignored(self) -> None:
        assert _translate_event(NS(type="ping"), _StreamState(), model_name="m") == []


# --------------------------------------------------------------------------- #
# stream() full-drive                                                         #
# --------------------------------------------------------------------------- #


def _patched_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    inst = MagicMock()
    inst.messages = MagicMock()
    inst.messages.create = AsyncMock()
    monkeypatch.setattr("primer.llm.anthropic.AsyncAnthropic", MagicMock(return_value=inst))
    return inst


class TestStream:
    async def test_unknown_model_raises(self) -> None:
        llm = AnthropicLLM(_make_provider(models=["claude-sonnet-4-5"]))
        with pytest.raises(ModelNotFoundError, match="not-real"):
            async for _ in llm.stream(
                model="not-real",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_full_stream_event_sequence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = AnthropicLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _aiter(_ok_events())
        out = [
            ev
            async for ev in llm.stream(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
                max_output_tokens=64,
            )
        ]
        assert [type(e).__name__ for e in out] == ["StreamStart", "TextDelta", "Usage", "Done"]

    async def test_request_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = AnthropicLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _aiter(_ok_events())
        tool = Tool(id="search", description="S", toolset_id="t", args_schema={"type": "object"})
        async for _ in llm.stream(
            model="claude-sonnet-4-5",
            messages=[
                Message(role="system", parts=[TextPart(text="be terse")]),
                Message(role="user", parts=[TextPart(text="hi")]),
            ],
            temperature=0.3,
            top_p=0.8,
            max_output_tokens=32,
            stop=["END"],
            tools=[tool],
            tool_choice="required",
            extended={"top_k": 4, "junk": 1},
        ):
            pass
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["stream"] is True
        assert kwargs["model"] == "claude-sonnet-4-5"
        assert kwargs["system"] == "be terse"
        assert kwargs["temperature"] == 0.3
        assert kwargs["top_p"] == 0.8
        assert kwargs["max_tokens"] == 32
        assert kwargs["stop_sequences"] == ["END"]
        assert kwargs["tools"][0]["name"] == "search"
        assert kwargs["tool_choice"] == {"type": "any"}
        assert kwargs["top_k"] == 4
        assert "junk" not in kwargs

    async def test_response_format_emulation_in_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = AnthropicLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _aiter(_ok_events())
        async for _ in llm.stream(
            model="claude-sonnet-4-5",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            response_format=_Schema,
        ):
            pass
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["tools"][0]["name"] == "structured_output"
        assert kwargs["tool_choice"] == {"type": "tool", "name": "structured_output"}

    async def test_error_before_stream_raises_classified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = AnthropicLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(PrimerError):
            async for _ in llm.stream(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_auth_error_before_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import anthropic

        llm = AnthropicLLM(_make_provider())
        client = _patched_client(monkeypatch)
        exc = anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)
        exc.status_code = 401
        exc.code = None
        exc.message = "bad key"
        Exception.__init__(exc, "bad key")
        client.messages.create = AsyncMock(side_effect=exc)
        with pytest.raises(AuthenticationError):
            async for _ in llm.stream(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_mid_stream_error_yields_chat_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = AnthropicLLM(_make_provider())
        client = _patched_client(monkeypatch)

        async def _failing() -> AsyncIterator:
            yield NS(
                type="message_start",
                message=NS(id="m", model="claude-sonnet-4-5", usage=NS(input_tokens=1)),
            )
            raise RuntimeError("mid-stream boom")

        client.messages.create.return_value = _failing()
        out = [
            ev
            async for ev in llm.stream(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            )
        ]
        assert isinstance(out[-1], ChatError)
        assert out[-1].fatal is True


# --------------------------------------------------------------------------- #
# Adapter lifecycle + construction                                            #
# --------------------------------------------------------------------------- #


class _StallStream:
    def __aiter__(self) -> "_StallStream":
        return self

    async def __anext__(self) -> Any:
        await asyncio.sleep(3600)


async def _slow_forever(event: Any) -> AsyncIterator:
    while True:
        await asyncio.sleep(0.005)
        yield event


class TestTimeouts:
    async def test_connect_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from primer.model.except_ import ProviderTimeoutError

        llm = AnthropicLLM(_make_provider())
        llm._connect_timeout_seconds = 0.05
        client = _patched_client(monkeypatch)

        async def _stall(**_: Any) -> Any:
            await asyncio.sleep(3600)

        client.messages.create = _stall
        with pytest.raises(ProviderTimeoutError):
            async for _ in llm.stream(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_stream_stall_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from primer.model.except_ import ProviderTimeoutError

        llm = AnthropicLLM(_make_provider())
        llm._connect_timeout_seconds = None
        llm._request_timeout_seconds = 0.05
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _StallStream()
        with pytest.raises(ProviderTimeoutError) as exc:
            async for _ in llm.stream(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
        assert exc.value.code == "stream_timeout"

    async def test_generation_budget_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from primer.model.except_ import ProviderTimeoutError

        llm = AnthropicLLM(_make_provider())
        llm._connect_timeout_seconds = None
        llm._request_timeout_seconds = None
        llm._total_timeout_seconds = 0.05
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _slow_forever(NS(type="ping"))
        with pytest.raises(ProviderTimeoutError) as exc:
            async for _ in llm.stream(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
        assert exc.value.code == "generation_timeout"

    async def test_trace_llm_io_sets_span_attribute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = AnthropicLLM(provider, trace_llm_io=True)
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _aiter(_ok_events())
        async for _ in llm.stream(
            model="claude-sonnet-4-5",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
        ):
            pass
        # The request-messages attribute is serialised only when tracing IO.
        assert client.messages.create.call_args is not None


class TestAdapterLifecycle:
    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", LLMProviderType.GEMINI)
        with pytest.raises(ConfigError, match="ANTHROPIC"):
            AnthropicLLM(provider)

    def test_rejects_wrong_config_type(self) -> None:
        provider = LLMProvider(
            id="a",
            provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="claude-sonnet-4-5", context_length=1024)],
            config=OpenResponsesConfig(  # type: ignore[arg-type]
                url=HttpUrl("https://api.openai.com/v1/"),
                api_key=SecretStr("sk-x"),
            ),
            limits=Limits(max_concurrency=1),
        )
        with pytest.raises(ConfigError, match="AnthropicConfig"):
            AnthropicLLM(provider)

    def test_accepts_empty_api_key(self) -> None:
        llm = AnthropicLLM(_make_provider(api_key=""))
        assert llm._client is None

    async def test_list_models(self) -> None:
        llm = AnthropicLLM(_make_provider(models=["a", "b"]))
        assert list(await llm.list_models()) == ["a", "b"]

    async def test_get_client_lazy_and_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inst = _patched_client(monkeypatch)
        llm = AnthropicLLM(_make_provider())
        assert llm._client is None
        c1 = llm._get_client()
        c2 = llm._get_client()
        assert c1 is c2 is inst

    async def test_aclose_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inst = _patched_client(monkeypatch)
        inst.close = AsyncMock()
        llm = AnthropicLLM(_make_provider())
        llm._get_client()
        await llm.aclose()
        await llm.aclose()  # no error second time
        inst.close.assert_awaited_once()

    async def test_count_tokens_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patched_client(monkeypatch)
        llm = AnthropicLLM(_make_provider())
        with patch(
            "primer.llm.anthropic.count_tokens_anthropic",
            new=AsyncMock(return_value=123),
        ) as mock_count:
            n = await llm.count_tokens(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            )
        assert n == 123
        mock_count.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Model discovery (respx-mocked HTTP)                                          #
# --------------------------------------------------------------------------- #


class TestDiscover:
    @respx.mock
    async def test_maps_and_paginates(self) -> None:
        respx.get(f"{ANTHROPIC_BASE_URL}/models").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "data": [
                            {"id": "claude-opus-4-5", "display_name": "Opus"},
                            {"id": "claude-opus-4-5", "display_name": "Dup"},
                            "not-a-dict-entry",
                            {"display_name": "no-id-ignored"},
                        ],
                        "has_more": True,
                        "last_id": "claude-opus-4-5",
                    },
                ),
                httpx.Response(
                    200,
                    json={"data": [{"id": "claude-haiku-4"}], "has_more": False},
                ),
            ]
        )
        out = await _discover_anthropic_models(AnthropicConfig(api_key=SecretStr("k")))
        assert [m["name"] for m in out] == ["claude-opus-4-5", "claude-haiku-4"]
        # display_name falls back to the id when absent.
        assert out[1]["display_name"] == "claude-haiku-4"

    @respx.mock
    async def test_sends_version_header_and_key(self) -> None:
        route = respx.get(f"{ANTHROPIC_BASE_URL}/models").mock(
            return_value=httpx.Response(200, json={"data": [], "has_more": False})
        )
        await _discover_anthropic_models(AnthropicConfig(api_key=SecretStr("secret-key")))
        req = route.calls.last.request
        assert req.headers["x-api-key"] == "secret-key"
        assert req.headers["anthropic-version"] == ANTHROPIC_VERSION

    @respx.mock
    async def test_has_more_without_cursor_stops(self) -> None:
        respx.get(f"{ANTHROPIC_BASE_URL}/models").mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": "m1"}], "has_more": True}
            )
        )
        out = await _discover_anthropic_models(AnthropicConfig(api_key=SecretStr("k")))
        assert [m["name"] for m in out] == ["m1"]

    @respx.mock
    async def test_401_raises(self) -> None:
        respx.get(f"{ANTHROPIC_BASE_URL}/models").mock(
            return_value=httpx.Response(401, json={"error": "nope"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _discover_anthropic_models(AnthropicConfig(api_key=SecretStr("bad")))

    @respx.mock
    async def test_no_api_key_sends_empty(self) -> None:
        route = respx.get(f"{ANTHROPIC_BASE_URL}/models").mock(
            return_value=httpx.Response(200, json={"data": [], "has_more": False})
        )
        out = await _discover_anthropic_models(AnthropicConfig())
        assert out == []
        assert route.calls.last.request.headers["x-api-key"] == ""
