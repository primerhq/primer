"""Unit tests for the Anthropic LLM adapter."""

from __future__ import annotations

import asyncio
import base64
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from matrix.llm.anthropic import (
    AnthropicLLM,
    _messages_to_anthropic,
    _part_to_anthropic_block,
)
from matrix.model.chat import (
    AudioPart,
    DocumentPart,
    ExtendedPart,
    ImagePart,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    VideoPart,
)
from matrix.model.except_ import ConfigError, UnsupportedContentError
from matrix.model.provider import (
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)


def _make_provider(
    *,
    api_key: str = "sk-ant-test",
    models: list[str] | None = None,
    max_concurrency: int = 4,
) -> LLMProvider:
    return LLMProvider(
        id="ant-default",
        provider=LLMProviderType.ANTHROPIC,
        models=[
            LLMModel(name=name, context_length=200_000)
            for name in (models or ["claude-sonnet-4-5"])
        ],
        config=AnthropicConfig(api_key=SecretStr(api_key)),
        limits=Limits(max_concurrency=max_concurrency),
    )


class TestConstructor:
    def test_accepts_valid_config(self) -> None:
        provider = _make_provider()
        llm = AnthropicLLM(provider)
        assert llm._client is None

    def test_accepts_empty_api_key(self) -> None:
        """An empty api_key (or None) is allowed at construction so
        operators can wire a proxy that injects auth elsewhere. The
        real Anthropic API will return 401 at call time if the key is
        actually needed — that's the natural surface for it."""
        provider = _make_provider(api_key="")
        llm = AnthropicLLM(provider)
        assert llm._client is None

    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", "openresponses")  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="ANTHROPIC"):
            AnthropicLLM(provider)

    def test_rejects_wrong_config_type(self) -> None:
        from pydantic import HttpUrl
        from matrix.model.provider import OpenResponsesConfig

        provider = LLMProvider(
            id="x",
            provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="claude-sonnet-4-5", context_length=1024)],
            config=OpenResponsesConfig(  # type: ignore[arg-type]
                url=HttpUrl("https://x/v1/"),
                api_key=SecretStr("sk-x"),
            ),
            limits=Limits(max_concurrency=1),
        )
        with pytest.raises(ConfigError, match="AnthropicConfig"):
            AnthropicLLM(provider)

    def test_initialises_semaphore(self) -> None:
        provider = _make_provider(max_concurrency=3)
        llm = AnthropicLLM(provider)
        assert isinstance(llm._semaphore, asyncio.Semaphore)
        assert llm._semaphore._value == 3  # type: ignore[attr-defined]

    def test_logs_init(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="matrix.llm.anthropic")
        provider = _make_provider(models=["claude-sonnet-4-5", "claude-opus-4-7"], max_concurrency=2)
        AnthropicLLM(provider)
        records = [r for r in caplog.records if "Anthropic adapter initialized" in r.message]
        assert len(records) == 1
        assert records[0].provider_id == "ant-default"  # type: ignore[attr-defined]


class TestListModels:
    async def test_returns_configured_names(self) -> None:
        provider = _make_provider(models=["m1", "m2"])
        llm = AnthropicLLM(provider)
        assert list(await llm.list_models()) == ["m1", "m2"]

    async def test_does_not_call_upstream(self) -> None:
        provider = _make_provider()
        llm = AnthropicLLM(provider)
        with patch.object(AnthropicLLM, "_get_client") as m:
            await llm.list_models()
            m.assert_not_called()


class TestPartToAnthropicBlock:
    def test_text_part(self) -> None:
        part = TextPart(text="hello")
        assert _part_to_anthropic_block(part) == {"type": "text", "text": "hello"}

    def test_image_part_data(self) -> None:
        part = ImagePart(data=b"\x89PNG", mime_type="image/png")
        out = _part_to_anthropic_block(part)
        assert out["type"] == "image"
        assert out["source"]["type"] == "base64"
        assert out["source"]["media_type"] == "image/png"
        assert base64.b64decode(out["source"]["data"]) == b"\x89PNG"

    def test_image_part_url(self) -> None:
        part = ImagePart(url="https://example.com/img.png")
        assert _part_to_anthropic_block(part) == {
            "type": "image",
            "source": {"type": "url", "url": "https://example.com/img.png"},
        }

    def test_image_part_file_id_raises(self) -> None:
        part = ImagePart(file_id="file-abc")
        with pytest.raises(UnsupportedContentError, match="file_id"):
            _part_to_anthropic_block(part)

    def test_document_part_data(self) -> None:
        part = DocumentPart(data=b"%PDF-1.4", mime_type="application/pdf")
        out = _part_to_anthropic_block(part)
        assert out["type"] == "document"
        assert out["source"]["type"] == "base64"
        assert out["source"]["media_type"] == "application/pdf"
        assert base64.b64decode(out["source"]["data"]) == b"%PDF-1.4"

    def test_document_part_url(self) -> None:
        part = DocumentPart(url="https://example.com/doc.pdf")
        assert _part_to_anthropic_block(part) == {
            "type": "document",
            "source": {"type": "url", "url": "https://example.com/doc.pdf"},
        }

    def test_document_part_file_id_raises(self) -> None:
        part = DocumentPart(file_id="file-xyz")
        with pytest.raises(UnsupportedContentError, match="file_id"):
            _part_to_anthropic_block(part)

    def test_tool_call_part(self) -> None:
        part = ToolCallPart(id="call_1", name="search", arguments={"q": "weather"})
        assert _part_to_anthropic_block(part) == {
            "type": "tool_use",
            "id": "call_1",
            "name": "search",
            "input": {"q": "weather"},
        }

    def test_audio_part_raises(self) -> None:
        part = ExtendedPart(extended=AudioPart(data=b"raw", mime_type="audio/mp3"))
        with pytest.raises(UnsupportedContentError, match="audio"):
            _part_to_anthropic_block(part)

    def test_video_part_raises(self) -> None:
        part = ExtendedPart(extended=VideoPart(url="https://example.com/v.mp4"))
        with pytest.raises(UnsupportedContentError, match="video"):
            _part_to_anthropic_block(part)


class TestMessagesToAnthropic:
    def test_empty_messages(self) -> None:
        assert _messages_to_anthropic([]) == (None, [])

    def test_simple_user_message(self) -> None:
        system, messages = _messages_to_anthropic(
            [Message(role="user", parts=[TextPart(text="hi")])]
        )
        assert system is None
        assert messages == [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        ]

    def test_system_message_returned_as_string(self) -> None:
        system, messages = _messages_to_anthropic(
            [
                Message(role="system", parts=[TextPart(text="be terse")]),
                Message(role="user", parts=[TextPart(text="hello")]),
            ]
        )
        assert system == "be terse"
        assert len(messages) == 1
        assert messages[0]["role"] == "user"

    def test_multiple_system_messages_concatenated(self) -> None:
        system, _ = _messages_to_anthropic(
            [
                Message(role="system", parts=[TextPart(text="be terse")]),
                Message(role="system", parts=[TextPart(text="be polite")]),
                Message(role="user", parts=[TextPart(text="hello")]),
            ]
        )
        assert system == "be terse\n\nbe polite"

    def test_system_with_non_text_part_raises(self) -> None:
        msg = Message.model_construct(
            role="system",
            parts=[ImagePart(url="https://example.com/img.png")],
        )
        with pytest.raises(UnsupportedContentError, match="system messages"):
            _messages_to_anthropic([msg])

    def test_assistant_with_text_and_tool_call(self) -> None:
        _, messages = _messages_to_anthropic(
            [
                Message(
                    role="assistant",
                    parts=[
                        TextPart(text="let me check"),
                        ToolCallPart(
                            id="call_1",
                            name="search",
                            arguments={"q": "weather"},
                        ),
                    ],
                )
            ]
        )
        assert messages == [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "let me check"},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "search",
                        "input": {"q": "weather"},
                    },
                ],
            }
        ]

    def test_tool_role_message_becomes_user_with_tool_result_blocks(self) -> None:
        _, messages = _messages_to_anthropic(
            [
                Message(
                    role="tool",
                    parts=[
                        ToolResultPart(id="call_1", output="42"),
                        ToolResultPart(id="call_2", output="failed", error=True),
                    ],
                )
            ]
        )
        assert messages == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": "42",
                        "is_error": False,
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_2",
                        "content": "failed",
                        "is_error": True,
                    },
                ],
            }
        ]

    def test_tool_role_with_non_tool_result_raises(self) -> None:
        msg = Message.model_construct(
            role="tool", parts=[TextPart(text="oops")]
        )
        with pytest.raises(UnsupportedContentError, match="tool-role messages"):
            _messages_to_anthropic([msg])


from typing import Any

from pydantic import BaseModel as PydanticBaseModel

from matrix.llm.anthropic import (
    _build_sampling_kwargs,
    _extract_extended_kwargs,
    _response_format_to_emulation,
    _tool_choice_to_anthropic,
    _tools_to_anthropic,
)
from matrix.model.chat import Tool


class TestTools:
    def test_none_returns_none(self) -> None:
        assert _tools_to_anthropic(None) is None

    def test_empty_list_returns_none(self) -> None:
        assert _tools_to_anthropic([]) is None

    def test_tool_to_anthropic(self) -> None:
        tool = Tool(
            id="get_weather",
            description="Get the weather",
            toolset_id="weather_kit",
            args_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        )
        out = _tools_to_anthropic([tool])
        assert out == [
            {
                "name": "get_weather",
                "description": "Get the weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ]
        # toolset_id is intentionally NOT transmitted
        assert "toolset_id" not in out[0]


class TestToolChoice:
    def test_none_returns_none(self) -> None:
        assert _tool_choice_to_anthropic(None) is None

    def test_auto(self) -> None:
        assert _tool_choice_to_anthropic("auto") == {"type": "auto"}

    def test_required_maps_to_any(self) -> None:
        assert _tool_choice_to_anthropic("required") == {"type": "any"}

    def test_none_string_maps_to_none_type(self) -> None:
        assert _tool_choice_to_anthropic("none") == {"type": "none"}

    def test_specific_tool_name(self) -> None:
        assert _tool_choice_to_anthropic("get_weather") == {
            "type": "tool",
            "name": "get_weather",
        }


class TestResponseFormat:
    def test_none_returns_none(self) -> None:
        assert (
            _response_format_to_emulation(
                None, has_tools=False, has_tool_choice=False
            )
            is None
        )

    def test_dict_schema(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        out = _response_format_to_emulation(
            schema, has_tools=False, has_tool_choice=False
        )
        assert out is not None
        synthetic_tools, synthetic_tool_choice = out
        assert synthetic_tools == [
            {
                "name": "structured_output",
                "description": "Emit the response in the structured shape defined by input_schema.",
                "input_schema": schema,
            }
        ]
        assert synthetic_tool_choice == {
            "type": "tool",
            "name": "structured_output",
        }

    def test_pydantic_class(self) -> None:
        class Answer(PydanticBaseModel):
            value: int

        out = _response_format_to_emulation(
            Answer, has_tools=False, has_tool_choice=False
        )
        assert out is not None
        synthetic_tools, synthetic_tool_choice = out
        assert synthetic_tools[0]["name"] == "structured_output"
        assert "value" in synthetic_tools[0]["input_schema"]["properties"]
        assert synthetic_tool_choice == {
            "type": "tool",
            "name": "structured_output",
        }

    def test_invalid_type_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="response_format"):
            _response_format_to_emulation(
                42,  # type: ignore[arg-type]
                has_tools=False,
                has_tool_choice=False,
            )

    def test_with_tools_raises_config_error(self) -> None:
        schema = {"type": "object"}
        with pytest.raises(ConfigError, match="cannot be combined with tools"):
            _response_format_to_emulation(
                schema, has_tools=True, has_tool_choice=False
            )

    def test_with_tool_choice_raises_config_error(self) -> None:
        schema = {"type": "object"}
        with pytest.raises(
            ConfigError, match="cannot be combined with explicit tool_choice"
        ):
            _response_format_to_emulation(
                schema, has_tools=False, has_tool_choice=True
            )


class TestSampling:
    def test_all_params_forwarded(self) -> None:
        out = _build_sampling_kwargs(
            temperature=0.7,
            top_p=0.9,
            max_output_tokens=500,
            stop=["END"],
        )
        assert out == {
            "temperature": 0.7,
            "top_p": 0.9,
            "max_tokens": 500,
            "stop_sequences": ["END"],
        }

    def test_max_tokens_default_when_none_with_info_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="matrix.llm.anthropic")
        out = _build_sampling_kwargs(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=None,
        )
        assert out == {"max_tokens": 4096}
        info_records = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "max_output_tokens" in r.message
        ]
        assert len(info_records) == 1

    def test_explicit_max_tokens_overrides_default(self) -> None:
        out = _build_sampling_kwargs(
            temperature=None,
            top_p=None,
            max_output_tokens=2048,
            stop=None,
        )
        assert out == {"max_tokens": 2048}


class TestExtendedKwargs:
    def test_none_input_returns_empty(self) -> None:
        assert _extract_extended_kwargs(None) == {}

    def test_empty_dict_returns_empty(self) -> None:
        assert _extract_extended_kwargs({}) == {}

    def test_unknown_keys_dropped_with_debug_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="matrix.llm.anthropic")
        out = _extract_extended_kwargs({"frobnicate": True, "foobar": 42})
        assert out == {}
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any(
            "frobnicate" in r.message and "foobar" in r.message
            for r in debug_records
        )

    @pytest.mark.parametrize(
        "key, value",
        [
            ("top_k", 40),
            ("metadata", {"trace_id": "x"}),
            ("service_tier", "flex"),
            ("cache_control", {"type": "ephemeral"}),
            ("thinking", {"type": "enabled", "budget_tokens": 1024}),
        ],
    )
    def test_recognised_keys_passthrough(self, key: str, value: Any) -> None:
        out = _extract_extended_kwargs({key: value})
        assert out == {key: value}


from types import SimpleNamespace as NS

from matrix.llm.anthropic import (
    _StreamState,
    _map_stop_reason,
    _translate_event,
)
from matrix.model.chat import (
    Citation,
    Done,
    ExtendedEvent,
    ReasoningDelta,
    ServerToolCallStart,
    StreamStart,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)


class TestStopReason:
    @pytest.mark.parametrize(
        "raw, saw_tool_use, expected",
        [
            ("end_turn", False, "stop"),
            ("end_turn", True, "tool_use"),
            ("max_tokens", False, "max_tokens"),
            ("stop_sequence", False, "stop_sequence"),
            ("tool_use", False, "tool_use"),
            ("pause_turn", False, "stop"),
            ("refusal", False, "content_filter"),
            ("something_unknown", False, "other"),
            (None, False, "other"),
        ],
    )
    def test_mapping(
        self, raw: str | None, saw_tool_use: bool, expected: str
    ) -> None:
        state = _StreamState()
        state.saw_tool_use = saw_tool_use
        assert _map_stop_reason(raw, state) == expected


class TestStreamMapping:
    def test_message_start_emits_stream_start_and_captures_input_tokens(
        self,
    ) -> None:
        state = _StreamState()
        ev = NS(
            type="message_start",
            message=NS(
                id="msg_123",
                model="claude-sonnet-4-5",
                usage=NS(input_tokens=42),
            ),
        )
        out = _translate_event(ev, state, model_name="claude-sonnet-4-5")
        assert len(out) == 1
        assert isinstance(out[0], StreamStart)
        assert out[0].request_id == "msg_123"
        assert out[0].model == "claude-sonnet-4-5"
        assert state.input_tokens == 42
        assert state.emitted_stream_start is True

    def test_message_start_falls_back_to_caller_model_when_sdk_omits(
        self,
    ) -> None:
        state = _StreamState()
        ev = NS(
            type="message_start",
            message=NS(id="msg_1", model=None, usage=NS(input_tokens=1)),
        )
        out = _translate_event(ev, state, model_name="caller-model")
        assert isinstance(out[0], StreamStart)
        assert out[0].model == "caller-model"

    def test_text_block_start_registers_kind_silently(self) -> None:
        state = _StreamState()
        ev = NS(
            type="content_block_start",
            index=0,
            content_block=NS(type="text"),
        )
        assert _translate_event(ev, state, model_name="m") == []
        assert state.block_kinds[0] == "text"

    def test_tool_use_block_start_emits_tool_call_start_and_sets_flag(
        self,
    ) -> None:
        state = _StreamState()
        ev = NS(
            type="content_block_start",
            index=1,
            content_block=NS(type="tool_use", id="tu_1", name="search"),
        )
        out = _translate_event(ev, state, model_name="m")
        assert len(out) == 1
        assert isinstance(out[0], ToolCallStart)
        assert out[0].id == "tu_1"
        assert out[0].name == "search"
        assert out[0].index == 1
        assert state.saw_tool_use is True
        assert state.tool_call_meta[1]["id"] == "tu_1"

    def test_thinking_block_start_registers_kind_silently(self) -> None:
        state = _StreamState()
        ev = NS(
            type="content_block_start",
            index=2,
            content_block=NS(type="thinking"),
        )
        assert _translate_event(ev, state, model_name="m") == []
        assert state.block_kinds[2] == "thinking"

    def test_server_tool_use_block_emits_extended_server_tool_call_start(
        self,
    ) -> None:
        state = _StreamState()
        ev = NS(
            type="content_block_start",
            index=3,
            content_block=NS(
                type="server_tool_use", id="stu_1", name="web_search"
            ),
        )
        out = _translate_event(ev, state, model_name="m")
        assert len(out) == 1
        assert isinstance(out[0], ExtendedEvent)
        assert isinstance(out[0].extended, ServerToolCallStart)
        assert out[0].extended.id == "stu_1"
        assert out[0].extended.tool_name == "web_search"
        assert out[0].extended.index == 3

    def test_text_delta(self) -> None:
        state = _StreamState()
        ev = NS(
            type="content_block_delta",
            index=0,
            delta=NS(type="text_delta", text="hello"),
        )
        out = _translate_event(ev, state, model_name="m")
        assert len(out) == 1
        assert isinstance(out[0], TextDelta)
        assert out[0].text == "hello"
        assert out[0].index == 0

    def test_input_json_delta_accumulates_and_emits_tool_call_delta(self) -> None:
        state = _StreamState()
        # Set up the tool_use block first
        _translate_event(
            NS(
                type="content_block_start",
                index=1,
                content_block=NS(
                    type="tool_use", id="tu_1", name="search"
                ),
            ),
            state,
            model_name="m",
        )
        out1 = _translate_event(
            NS(
                type="content_block_delta",
                index=1,
                delta=NS(type="input_json_delta", partial_json='{"q":'),
            ),
            state,
            model_name="m",
        )
        out2 = _translate_event(
            NS(
                type="content_block_delta",
                index=1,
                delta=NS(type="input_json_delta", partial_json='"hi"}'),
            ),
            state,
            model_name="m",
        )
        assert isinstance(out1[0], ToolCallDelta)
        assert out1[0].id == "tu_1"
        assert out1[0].arguments_delta == '{"q":'
        assert isinstance(out2[0], ToolCallDelta)
        assert out2[0].arguments_delta == '"hi"}'
        assert state.accumulated_args[1] == '{"q":"hi"}'

    def test_thinking_delta_maps_to_reasoning_delta(self) -> None:
        state = _StreamState()
        ev = NS(
            type="content_block_delta",
            index=2,
            delta=NS(type="thinking_delta", thinking="pondering..."),
        )
        out = _translate_event(ev, state, model_name="m")
        assert isinstance(out[0], ReasoningDelta)
        assert out[0].text == "pondering..."
        assert out[0].index == 2
        assert out[0].signature is None

    def test_signature_delta_maps_to_reasoning_delta_with_signature(self) -> None:
        state = _StreamState()
        ev = NS(
            type="content_block_delta",
            index=2,
            delta=NS(type="signature_delta", signature="sig_xyz"),
        )
        out = _translate_event(ev, state, model_name="m")
        assert isinstance(out[0], ReasoningDelta)
        assert out[0].text == ""
        assert out[0].signature == "sig_xyz"

    def test_citations_delta_maps_to_extended_citation(self) -> None:
        state = _StreamState()
        ev = NS(
            type="content_block_delta",
            index=0,
            delta=NS(
                type="citations_delta",
                citation=NS(
                    type="char_location",
                    cited_text="quoted snippet",
                    document_title="My Doc",
                    file_id="file_1",
                    start_char_index=10,
                    end_char_index=24,
                ),
            ),
        )
        out = _translate_event(ev, state, model_name="m")
        assert isinstance(out[0], ExtendedEvent)
        assert isinstance(out[0].extended, Citation)
        assert out[0].extended.quoted_text == "quoted snippet"
        assert out[0].extended.source_title == "My Doc"
        assert out[0].extended.source_id == "file_1"
        assert out[0].extended.start_index == 10
        assert out[0].extended.end_index == 24
        assert out[0].extended.index == 0

    def test_content_block_stop_for_tool_use_parses_accumulated_json(
        self,
    ) -> None:
        state = _StreamState()
        _translate_event(
            NS(
                type="content_block_start",
                index=1,
                content_block=NS(
                    type="tool_use", id="tu_1", name="search"
                ),
            ),
            state,
            model_name="m",
        )
        _translate_event(
            NS(
                type="content_block_delta",
                index=1,
                delta=NS(
                    type="input_json_delta", partial_json='{"q":"hi"}'
                ),
            ),
            state,
            model_name="m",
        )
        out = _translate_event(
            NS(type="content_block_stop", index=1),
            state,
            model_name="m",
        )
        assert len(out) == 1
        assert isinstance(out[0], ToolCallEnd)
        assert out[0].id == "tu_1"
        assert out[0].arguments == {"q": "hi"}
        assert out[0].index == 1

    def test_content_block_stop_for_text_emits_nothing(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(
                type="content_block_start",
                index=0,
                content_block=NS(type="text"),
            ),
            state,
            model_name="m",
        )
        out = _translate_event(
            NS(type="content_block_stop", index=0), state, model_name="m"
        )
        assert out == []

    def test_content_block_stop_with_invalid_json_yields_empty_args(
        self,
    ) -> None:
        state = _StreamState()
        _translate_event(
            NS(
                type="content_block_start",
                index=1,
                content_block=NS(
                    type="tool_use", id="tu_1", name="search"
                ),
            ),
            state,
            model_name="m",
        )
        # Inject malformed JSON directly via accumulated_args.
        state.accumulated_args[1] = "{not json"
        out = _translate_event(
            NS(type="content_block_stop", index=1), state, model_name="m"
        )
        assert isinstance(out[0], ToolCallEnd)
        assert out[0].arguments == {}

    def test_message_delta_captures_stop_reason_and_output_tokens(self) -> None:
        state = _StreamState()
        ev = NS(
            type="message_delta",
            delta=NS(stop_reason="end_turn"),
            usage=NS(output_tokens=99),
        )
        assert _translate_event(ev, state, model_name="m") == []
        assert state.final_stop_reason == "end_turn"
        assert state.output_tokens == 99

    def test_message_stop_emits_usage_then_done(self) -> None:
        state = _StreamState()
        state.input_tokens = 10
        state.output_tokens = 20
        state.final_stop_reason = "end_turn"
        out = _translate_event(
            NS(type="message_stop"), state, model_name="m"
        )
        assert len(out) == 2
        assert isinstance(out[0], Usage)
        assert out[0].input_tokens == 10
        assert out[0].output_tokens == 20
        assert out[0].cumulative is False
        assert isinstance(out[1], Done)
        assert out[1].stop_reason == "stop"
        assert out[1].raw_reason == "end_turn"

    def test_message_stop_omits_usage_when_tokens_missing(self) -> None:
        state = _StreamState()
        state.final_stop_reason = "end_turn"
        out = _translate_event(
            NS(type="message_stop"), state, model_name="m"
        )
        assert len(out) == 1
        assert isinstance(out[0], Done)

    def test_unknown_event_type_returns_empty_list(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(type="some_future_event"), state, model_name="m"
        )
        assert out == []


from collections.abc import AsyncIterator

import anthropic

from matrix.model.chat import Error as ChatError
from matrix.model.except_ import AuthenticationError, ModelNotFoundError


async def _aiter(items: list) -> AsyncIterator:
    for item in items:
        yield item


def _patched_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the AsyncAnthropic symbol in the adapter module to a MagicMock.

    Returns the mock instance the adapter will see when it constructs
    the client. Tests configure ``mock.messages.create`` to drive the
    SDK behaviour.
    """
    mock_instance = MagicMock()
    mock_instance.messages = MagicMock()
    mock_instance.messages.create = AsyncMock()
    cls_mock = MagicMock(return_value=mock_instance)
    monkeypatch.setattr("matrix.llm.anthropic.AsyncAnthropic", cls_mock)
    return mock_instance


def _make_anthropic_error(
    cls: type, *, status_code: int = 400, code: str | None = None
):
    """Build an anthropic SDK exception with minimal init plumbing."""
    exc = cls.__new__(cls)
    exc.status_code = status_code
    exc.code = code
    exc.message = f"test {cls.__name__}"
    Exception.__init__(exc, exc.message)
    return exc


def _ok_events() -> list[Any]:
    """A minimal valid Anthropic stream — message_start ... message_stop."""
    return [
        NS(
            type="message_start",
            message=NS(
                id="msg_1",
                model="claude-sonnet-4-5",
                usage=NS(input_tokens=5),
            ),
        ),
        NS(
            type="content_block_start",
            index=0,
            content_block=NS(type="text"),
        ),
        NS(
            type="content_block_delta",
            index=0,
            delta=NS(type="text_delta", text="hi"),
        ),
        NS(type="content_block_stop", index=0),
        NS(
            type="message_delta",
            delta=NS(stop_reason="end_turn"),
            usage=NS(output_tokens=3),
        ),
        NS(type="message_stop"),
    ]


class TestStream:
    async def test_unknown_model_raises_model_not_found(self) -> None:
        provider = _make_provider(models=["claude-sonnet-4-5"])
        llm = AnthropicLLM(provider)
        with pytest.raises(ModelNotFoundError, match="not-a-real-model"):
            async for _ in llm.stream(
                model="not-a-real-model",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_full_stream_emits_start_text_usage_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = AnthropicLLM(provider)
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _aiter(_ok_events())

        out = [
            ev
            async for ev in llm.stream(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hello")])],
                max_output_tokens=64,
            )
        ]
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["StreamStart", "TextDelta", "Usage", "Done"]

    async def test_max_tokens_default_with_log(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger="matrix.llm.anthropic")
        provider = _make_provider()
        llm = AnthropicLLM(provider)
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _aiter(_ok_events())

        async for _ in llm.stream(
            model="claude-sonnet-4-5",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
        ):
            pass
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["max_tokens"] == 4096
        info_records = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "max_output_tokens" in r.message
        ]
        assert len(info_records) == 1

    async def test_system_messages_lifted_to_top_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = AnthropicLLM(provider)
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _aiter(_ok_events())

        async for _ in llm.stream(
            model="claude-sonnet-4-5",
            messages=[
                Message(role="system", parts=[TextPart(text="be terse")]),
                Message(role="user", parts=[TextPart(text="hello")]),
            ],
            max_output_tokens=64,
        ):
            pass
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["system"] == "be terse"
        assert all(m["role"] != "system" for m in kwargs["messages"])
        assert kwargs["stream"] is True

    async def test_tools_included_in_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = AnthropicLLM(provider)
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _aiter(_ok_events())

        tool = Tool(
            id="search",
            description="Search",
            toolset_id="default",
            args_schema={"type": "object", "properties": {}, "required": []},
        )
        async for _ in llm.stream(
            model="claude-sonnet-4-5",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            tools=[tool],
            tool_choice="auto",
            max_output_tokens=64,
        ):
            pass
        kwargs = client.messages.create.call_args.kwargs
        assert len(kwargs["tools"]) == 1
        assert kwargs["tools"][0]["name"] == "search"
        assert kwargs["tool_choice"] == {"type": "auto"}


class TestExceptionWrapping:
    async def test_pre_stream_auth_error_re_raised_as_matrix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = AnthropicLLM(provider)
        client = _patched_client(monkeypatch)
        client.messages.create.side_effect = _make_anthropic_error(
            anthropic.AuthenticationError, status_code=401
        )
        with pytest.raises(AuthenticationError):
            async for _ in llm.stream(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
                max_output_tokens=64,
            ):
                pass

    async def test_mid_stream_rate_limit_yields_terminal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = AnthropicLLM(provider)
        client = _patched_client(monkeypatch)

        async def failing_iter() -> AsyncIterator:
            yield NS(
                type="message_start",
                message=NS(
                    id="msg_1",
                    model="claude-sonnet-4-5",
                    usage=NS(input_tokens=5),
                ),
            )
            raise _make_anthropic_error(
                anthropic.RateLimitError, status_code=429
            )

        client.messages.create.return_value = failing_iter()
        events = [
            ev
            async for ev in llm.stream(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
                max_output_tokens=64,
            )
        ]
        assert isinstance(events[-1], ChatError)
        assert events[-1].fatal is True
        assert isinstance(events[0], StreamStart)


class TestConcurrency:
    async def test_semaphore_serialises_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(max_concurrency=1)
        llm = AnthropicLLM(provider)
        client = _patched_client(monkeypatch)

        in_flight = 0
        peak = 0

        async def slow_iter() -> AsyncIterator:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            for ev in _ok_events():
                yield ev
            in_flight -= 1

        client.messages.create.side_effect = lambda **_: slow_iter()

        async def consume() -> None:
            async for _ in llm.stream(
                model="claude-sonnet-4-5",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
                max_output_tokens=64,
            ):
                pass

        await asyncio.gather(consume(), consume(), consume())
        assert peak == 1


class TestPackageReexport:
    def test_anthropic_llm_reexported_from_package(self) -> None:
        import matrix.llm as llm_pkg

        assert "AnthropicLLM" in llm_pkg.__all__
        assert llm_pkg.AnthropicLLM is AnthropicLLM
