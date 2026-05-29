"""Unit tests for the OpenChat LLM adapter."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import HttpUrl, SecretStr

from primer.llm.openchat import OpenChatLLM, _POLICY_BY_FLAVOR, _FlavorPolicy
from primer.model.except_ import ConfigError
from primer.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)


def _make_provider(
    *,
    flavor: OpenChatFlavor = OpenChatFlavor.OPENAI,
    api_key: str | None = "sk-test",
    models: list[str] | None = None,
    max_concurrency: int = 4,
    url: str = "https://api.openai.com/v1/",
) -> LLMProvider:
    return LLMProvider(
        id="openchat-default",
        provider=LLMProviderType.OPENCHAT,
        models=[
            LLMModel(name=name, context_length=8192)
            for name in (models or ["gpt-4o-mini"])
        ],
        config=OpenChatConfig(
            url=HttpUrl(url),
            api_key=SecretStr(api_key) if api_key is not None else None,
            flavor=flavor,
        ),
        limits=Limits(max_concurrency=max_concurrency),
    )


class TestFlavorPolicy:
    def test_openai_policy_requires_api_key(self) -> None:
        assert _POLICY_BY_FLAVOR[OpenChatFlavor.OPENAI].require_api_key is True

    def test_lmstudio_policy_tolerates_no_key(self) -> None:
        assert _POLICY_BY_FLAVOR[OpenChatFlavor.LMSTUDIO].require_api_key is False

    def test_ollama_policy_tolerates_no_key(self) -> None:
        assert _POLICY_BY_FLAVOR[OpenChatFlavor.OLLAMA].require_api_key is False

    def test_vllm_policy_tolerates_no_key(self) -> None:
        assert _POLICY_BY_FLAVOR[OpenChatFlavor.VLLM].require_api_key is False

    def test_other_policy_requires_api_key(self) -> None:
        assert _POLICY_BY_FLAVOR[OpenChatFlavor.OTHER].require_api_key is True

    def test_policy_dataclass_is_frozen(self) -> None:
        policy = _POLICY_BY_FLAVOR[OpenChatFlavor.OPENAI]
        with pytest.raises(Exception):
            policy.require_api_key = False  # type: ignore[misc]


class TestConstructor:
    def test_accepts_valid_openai_config(self) -> None:
        llm = OpenChatLLM(_make_provider(flavor=OpenChatFlavor.OPENAI))
        assert llm._policy is _POLICY_BY_FLAVOR[OpenChatFlavor.OPENAI]
        assert llm._client is None

    def test_accepts_lmstudio_with_no_key(self) -> None:
        llm = OpenChatLLM(
            _make_provider(
                flavor=OpenChatFlavor.LMSTUDIO,
                api_key=None,
                url="http://localhost:1234/v1/",
            )
        )
        assert llm._policy.require_api_key is False

    def test_accepts_ollama_with_no_key(self) -> None:
        llm = OpenChatLLM(
            _make_provider(
                flavor=OpenChatFlavor.OLLAMA,
                api_key=None,
                url="http://localhost:11434/v1/",
            )
        )
        assert llm._policy.require_api_key is False

    def test_accepts_vllm_with_no_key(self) -> None:
        llm = OpenChatLLM(
            _make_provider(
                flavor=OpenChatFlavor.VLLM,
                api_key=None,
                url="http://localhost:8000/v1/",
            )
        )
        assert llm._policy.require_api_key is False

    def test_rejects_empty_api_key_for_openai_flavor(self) -> None:
        with pytest.raises(ConfigError, match="api_key is required"):
            OpenChatLLM(_make_provider(flavor=OpenChatFlavor.OPENAI, api_key=""))

    def test_rejects_missing_api_key_for_other_flavor(self) -> None:
        with pytest.raises(ConfigError, match="api_key is required"):
            OpenChatLLM(
                _make_provider(
                    flavor=OpenChatFlavor.OTHER,
                    api_key=None,
                    url="https://api.example.com/v1/",
                )
            )

    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", LLMProviderType.OPENRESPONSES)
        with pytest.raises(ConfigError, match="OPENCHAT"):
            OpenChatLLM(provider)

    def test_logs_init_with_structured_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="primer.llm.openchat")
        OpenChatLLM(
            _make_provider(models=["gpt-4o-mini", "gpt-4o"], max_concurrency=2)
        )
        records = [r for r in caplog.records if "OpenChat adapter" in r.message]
        assert len(records) == 1
        record = records[0]
        assert record.provider_id == "openchat-default"  # type: ignore[attr-defined]
        assert record.flavor == "openai"  # type: ignore[attr-defined]
        assert record.models == ["gpt-4o-mini", "gpt-4o"]  # type: ignore[attr-defined]
        assert record.max_concurrency == 2  # type: ignore[attr-defined]


class TestListModels:
    async def test_returns_configured_model_names(self) -> None:
        llm = OpenChatLLM(_make_provider(models=["gpt-4o-mini", "gpt-4o"]))
        assert list(await llm.list_models()) == ["gpt-4o-mini", "gpt-4o"]

    async def test_does_not_call_upstream(self) -> None:
        llm = OpenChatLLM(_make_provider())
        with patch.object(OpenChatLLM, "_get_client") as mock_get_client:
            await llm.list_models()
            mock_get_client.assert_not_called()


import base64

from primer.llm.openchat import _part_to_content
from primer.model.chat import (
    AudioPart,
    DocumentPart,
    ExtendedPart,
    ImagePart,
    TextPart,
    VideoPart,
)
from primer.model.except_ import UnsupportedContentError


class TestPartToContent:
    def test_text_part(self) -> None:
        assert _part_to_content(TextPart(text="hi")) == {
            "type": "text",
            "text": "hi",
        }

    def test_image_part_url(self) -> None:
        out = _part_to_content(ImagePart(url="https://example.com/x.png"))
        assert out == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/x.png"},
        }

    def test_image_part_url_includes_detail_when_set(self) -> None:
        out = _part_to_content(ImagePart(url="https://example.com/x.png", detail="high"))
        assert out == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/x.png", "detail": "high"},
        }

    def test_image_part_data_emits_data_uri(self) -> None:
        out = _part_to_content(ImagePart(data=b"\x89PNG", mime_type="image/png"))
        assert out["type"] == "image_url"
        url = out["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG"

    def test_image_part_data_defaults_mime(self) -> None:
        out = _part_to_content(ImagePart(data=b"raw"))
        assert out["image_url"]["url"].startswith("data:application/octet-stream;base64,")

    def test_image_part_file_id_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="file_id"):
            _part_to_content(ImagePart(file_id="file-abc"))

    def test_document_part_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="document"):
            _part_to_content(
                DocumentPart(data=b"%PDF", mime_type="application/pdf")
            )

    def test_audio_extended_part_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="audio"):
            _part_to_content(
                ExtendedPart(extended=AudioPart(data=b"x", mime_type="audio/mp3"))
            )

    def test_video_extended_part_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="video"):
            _part_to_content(
                ExtendedPart(extended=VideoPart(url="https://example.com/v.mp4"))
            )


from primer.llm.openchat import _messages_to_chat
from primer.model.chat import (
    Message,
    ToolCallPart,
    ToolResultPart,
)


class TestMessagesToChat:
    def test_simple_user_text_uses_string_content(self) -> None:
        rows = _messages_to_chat(
            [Message(role="user", parts=[TextPart(text="hi")])]
        )
        assert rows == [{"role": "user", "content": "hi"}]

    def test_user_with_image_uses_content_array(self) -> None:
        rows = _messages_to_chat(
            [
                Message(
                    role="user",
                    parts=[
                        TextPart(text="what is this?"),
                        ImagePart(url="https://example.com/x.png"),
                    ],
                )
            ]
        )
        assert rows == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/x.png"},
                    },
                ],
            }
        ]

    def test_system_message_passes_through(self) -> None:
        rows = _messages_to_chat(
            [Message(role="system", parts=[TextPart(text="be terse")])]
        )
        assert rows == [{"role": "system", "content": "be terse"}]

    def test_assistant_text_only(self) -> None:
        rows = _messages_to_chat(
            [Message(role="assistant", parts=[TextPart(text="ok")])]
        )
        assert rows == [{"role": "assistant", "content": "ok"}]

    def test_assistant_tool_calls_only_emits_null_content(self) -> None:
        rows = _messages_to_chat(
            [
                Message(
                    role="assistant",
                    parts=[
                        ToolCallPart(
                            id="call_1", name="search", arguments={"q": "weather"}
                        )
                    ],
                )
            ]
        )
        assert rows == [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": '{"q": "weather"}',
                        },
                    }
                ],
            }
        ]

    def test_assistant_text_and_tool_calls_combined(self) -> None:
        rows = _messages_to_chat(
            [
                Message(
                    role="assistant",
                    parts=[
                        TextPart(text="let me check"),
                        ToolCallPart(id="call_1", name="search", arguments={}),
                    ],
                )
            ]
        )
        assert rows == [
            {
                "role": "assistant",
                "content": "let me check",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }
                ],
            }
        ]

    def test_tool_role_message_emits_one_row_per_result(self) -> None:
        rows = _messages_to_chat(
            [
                Message(
                    role="tool",
                    parts=[
                        ToolResultPart(id="call_1", output="42"),
                        ToolResultPart(id="call_2", output="done"),
                    ],
                )
            ]
        )
        assert rows == [
            {"role": "tool", "tool_call_id": "call_1", "content": "42"},
            {"role": "tool", "tool_call_id": "call_2", "content": "done"},
        ]

    def test_tool_role_with_non_tool_result_raises(self) -> None:
        msg = Message.model_construct(role="tool", parts=[TextPart(text="oops")])
        with pytest.raises(UnsupportedContentError, match="ToolResultPart"):
            _messages_to_chat([msg])

    def test_full_conversation_round_trip(self) -> None:
        messages = [
            Message(role="system", parts=[TextPart(text="be helpful")]),
            Message(role="user", parts=[TextPart(text="weather?")]),
            Message(
                role="assistant",
                parts=[
                    ToolCallPart(
                        id="call_1", name="get_weather", arguments={"city": "NYC"}
                    )
                ],
            ),
            Message(
                role="tool", parts=[ToolResultPart(id="call_1", output="sunny")]
            ),
            Message(role="assistant", parts=[TextPart(text="It's sunny.")]),
        ]
        rows = _messages_to_chat(messages)
        roles = [r["role"] for r in rows]
        assert roles == ["system", "user", "assistant", "tool", "assistant"]
        assert rows[2]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert rows[3]["tool_call_id"] == "call_1"


from primer.llm.openchat import _tool_choice_to_chat, _tool_to_chat
from primer.model.chat import Tool


class TestTools:
    def test_tool_to_chat_wraps_function_envelope(self) -> None:
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
        out = _tool_to_chat(tool)
        assert out == {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
        assert "toolset_id" not in out["function"]


class TestToolChoice:
    def test_none_returns_none(self) -> None:
        assert _tool_choice_to_chat(None) is None

    @pytest.mark.parametrize("mode", ["auto", "required", "none"])
    def test_mode_strings_pass_through(self, mode: str) -> None:
        assert _tool_choice_to_chat(mode) == mode

    def test_specific_tool_name_wraps_with_function_nesting(self) -> None:
        assert _tool_choice_to_chat("get_weather") == {
            "type": "function",
            "function": {"name": "get_weather"},
        }


from pydantic import BaseModel as PydanticBaseModel

from primer.llm.openchat import (
    _build_sampling_params,
    _extract_extended_kwargs,
    _response_format_to_param,
)


class TestSampling:
    def test_all_params_forwarded_chat_keys(self) -> None:
        params = _build_sampling_params(
            temperature=0.7,
            top_p=0.9,
            max_output_tokens=500,
            stop=None,
        )
        assert params == {
            "temperature": 0.7,
            "top_p": 0.9,
            "max_tokens": 500,
        }

    def test_stop_passes_through_natively_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="primer.llm._openai_common")
        params = _build_sampling_params(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=["END", "\n"],
        )
        assert params == {"stop": ["END", "\n"]}
        assert not any("stop" in r.message.lower() for r in caplog.records)

    def test_all_none_returns_empty(self) -> None:
        assert _build_sampling_params(
            temperature=None, top_p=None, max_output_tokens=None, stop=None,
        ) == {}


class TestExtendedKwargs:
    @pytest.mark.parametrize(
        "key, value",
        [
            ("parallel_tool_calls", False),
            ("presence_penalty", 0.5),
            ("frequency_penalty", -0.25),
            ("logprobs", True),
            ("top_logprobs", 3),
            ("seed", 7),
            ("user", "u-123"),
        ],
    )
    def test_recognised_keys_passthrough(self, key: str, value: Any) -> None:
        assert _extract_extended_kwargs({key: value}) == {key: value}

    def test_unknown_keys_dropped_with_debug_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.openchat")
        out = _extract_extended_kwargs({"frobnicate": True, "foobar": 42})
        assert out == {}
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any(
            "frobnicate" in r.message and "foobar" in r.message
            for r in debug_records
        )

    def test_reasoning_effort_dropped_as_unknown(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.openchat")
        assert _extract_extended_kwargs({"reasoning_effort": "high"}) == {}

    def test_none_returns_empty(self) -> None:
        assert _extract_extended_kwargs(None) == {}

    def test_empty_dict_returns_empty(self) -> None:
        assert _extract_extended_kwargs({}) == {}


class TestResponseFormat:
    def test_none_returns_none(self) -> None:
        assert _response_format_to_param(None) is None

    def test_dict_schema_root_level_json_schema(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        out = _response_format_to_param(schema)
        assert out == {
            "type": "json_schema",
            "json_schema": {
                "name": "schema",
                "schema": schema,
                "strict": True,
            },
        }

    def test_pydantic_class(self) -> None:
        class Answer(PydanticBaseModel):
            value: int

        out = _response_format_to_param(Answer)
        assert out["type"] == "json_schema"
        assert out["json_schema"]["name"] == "Answer"
        assert "value" in out["json_schema"]["schema"]["properties"]
        assert out["json_schema"]["strict"] is True

    def test_invalid_type_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="response_format"):
            _response_format_to_param(42)  # type: ignore[arg-type]


from types import SimpleNamespace as NS

from primer.llm.openchat import (
    _StreamState,
    _build_usage,
    _map_finish_reason,
    _translate_chunk,
)
from primer.model.chat import (
    Done,
    StreamStart,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)


class TestStopReason:
    def test_stop_maps_to_stop(self) -> None:
        assert _map_finish_reason("stop") == "stop"

    def test_length_maps_to_max_tokens(self) -> None:
        assert _map_finish_reason("length") == "max_tokens"

    def test_tool_calls_maps_to_tool_use(self) -> None:
        assert _map_finish_reason("tool_calls") == "tool_use"

    def test_content_filter_maps_to_content_filter(self) -> None:
        assert _map_finish_reason("content_filter") == "content_filter"

    def test_unknown_maps_to_other(self) -> None:
        assert _map_finish_reason("weird") == "other"

    def test_none_maps_to_other(self) -> None:
        assert _map_finish_reason(None) == "other"


class TestStreamMapping:
    def test_first_chunk_with_role_emits_stream_start(self) -> None:
        state = _StreamState()
        chunk = NS(
            id="chatcmpl-1",
            model="gpt-4o-mini",
            choices=[NS(index=0, delta=NS(role="assistant", content=None, tool_calls=None), finish_reason=None)],
            usage=None,
        )
        out = _translate_chunk(chunk, state)
        assert len(out) == 1
        assert isinstance(out[0], StreamStart)
        assert out[0].request_id == "chatcmpl-1"
        assert out[0].model == "gpt-4o-mini"
        assert state.stream_started is True

    def test_subsequent_text_only_chunk_emits_text_delta(self) -> None:
        state = _StreamState()
        _translate_chunk(
            NS(
                id="chatcmpl-1",
                model="gpt-4o-mini",
                choices=[NS(index=0, delta=NS(role="assistant", content=None, tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            state,
        )
        chunk = NS(
            id="chatcmpl-1",
            model="gpt-4o-mini",
            choices=[NS(index=0, delta=NS(role=None, content="hi", tool_calls=None), finish_reason=None)],
            usage=None,
        )
        out = _translate_chunk(chunk, state)
        assert len(out) == 1
        assert isinstance(out[0], TextDelta)
        assert out[0].text == "hi"

    def test_text_delta_when_role_arrives_in_same_chunk_emits_start_then_text(self) -> None:
        state = _StreamState()
        chunk = NS(
            id="chatcmpl-1",
            model="gpt-4o-mini",
            choices=[NS(index=0, delta=NS(role="assistant", content="hi", tool_calls=None), finish_reason=None)],
            usage=None,
        )
        out = _translate_chunk(chunk, state)
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["StreamStart", "TextDelta"]

    def test_tool_call_start_emits_tool_call_start(self) -> None:
        state = _StreamState()
        _translate_chunk(
            NS(
                id="x",
                model="m",
                choices=[NS(index=0, delta=NS(role="assistant", content=None, tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            state,
        )
        chunk = NS(
            id="x",
            model="m",
            choices=[
                NS(
                    index=0,
                    delta=NS(
                        role=None,
                        content=None,
                        tool_calls=[
                            NS(
                                index=0,
                                id="call_a",
                                type="function",
                                function=NS(name="search", arguments=""),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        out = _translate_chunk(chunk, state)
        assert len(out) == 1
        assert isinstance(out[0], ToolCallStart)
        assert out[0].id == "call_a"
        assert out[0].name == "search"
        assert state.saw_function_call is True

    def test_tool_call_arguments_delta_after_start(self) -> None:
        state = _StreamState()
        _translate_chunk(
            NS(
                id="x",
                model="m",
                choices=[NS(index=0, delta=NS(role="assistant", content=None, tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            state,
        )
        _translate_chunk(
            NS(
                id="x",
                model="m",
                choices=[
                    NS(
                        index=0,
                        delta=NS(
                            role=None,
                            content=None,
                            tool_calls=[
                                NS(
                                    index=0,
                                    id="call_a",
                                    type="function",
                                    function=NS(name="search", arguments=""),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            state,
        )
        chunk = NS(
            id="x",
            model="m",
            choices=[
                NS(
                    index=0,
                    delta=NS(
                        role=None,
                        content=None,
                        tool_calls=[
                            NS(
                                index=0,
                                id=None,
                                type=None,
                                function=NS(name=None, arguments='{"q":'),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        out = _translate_chunk(chunk, state)
        assert len(out) == 1
        assert isinstance(out[0], ToolCallDelta)
        assert out[0].arguments_delta == '{"q":'
        assert out[0].id == "call_a"

    def test_finish_reason_tool_calls_flushes_tool_call_end_then_done(self) -> None:
        state = _StreamState()
        _translate_chunk(
            NS(
                id="x",
                model="m",
                choices=[NS(index=0, delta=NS(role="assistant", content=None, tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            state,
        )
        _translate_chunk(
            NS(
                id="x",
                model="m",
                choices=[
                    NS(
                        index=0,
                        delta=NS(
                            role=None,
                            content=None,
                            tool_calls=[
                                NS(
                                    index=0,
                                    id="call_a",
                                    type="function",
                                    function=NS(name="search", arguments='{"q":"weather"}'),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            state,
        )
        chunk = NS(
            id="x",
            model="m",
            choices=[
                NS(
                    index=0,
                    delta=NS(role=None, content=None, tool_calls=None),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        )
        out = _translate_chunk(chunk, state)
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["ToolCallEnd", "Done"]
        end = out[0]
        assert isinstance(end, ToolCallEnd)
        assert end.id == "call_a"
        assert end.arguments == {"q": "weather"}
        done = out[1]
        assert isinstance(done, Done)
        assert done.stop_reason == "tool_use"
        assert done.raw_reason == "tool_calls"

    def test_finish_reason_stop_emits_done_without_tool_end(self) -> None:
        state = _StreamState()
        _translate_chunk(
            NS(
                id="x",
                model="m",
                choices=[NS(index=0, delta=NS(role="assistant", content="hi", tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            state,
        )
        chunk = NS(
            id="x",
            model="m",
            choices=[
                NS(
                    index=0,
                    delta=NS(role=None, content=None, tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
        out = _translate_chunk(chunk, state)
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["Done"]
        assert out[0].stop_reason == "stop"

    def test_final_chunk_with_usage_emits_usage_then_done(self) -> None:
        state = _StreamState()
        _translate_chunk(
            NS(
                id="x",
                model="m",
                choices=[NS(index=0, delta=NS(role="assistant", content="hi", tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            state,
        )
        chunk = NS(
            id="x",
            model="m",
            choices=[
                NS(
                    index=0,
                    delta=NS(role=None, content=None, tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=NS(prompt_tokens=10, completion_tokens=5),
        )
        out = _translate_chunk(chunk, state)
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["Usage", "Done"]
        usage = out[0]
        assert isinstance(usage, Usage)
        assert usage.input_tokens == 10
        assert usage.output_tokens == 5
        assert usage.cumulative is False

    def test_trailing_usage_only_chunk_no_choices(self) -> None:
        state = _StreamState()
        state.stream_started = True
        chunk = NS(
            id="x",
            model="m",
            choices=[],
            usage=NS(prompt_tokens=8, completion_tokens=3),
        )
        out = _translate_chunk(chunk, state)
        assert len(out) == 1
        assert isinstance(out[0], Usage)
        assert out[0].input_tokens == 8

    def test_build_usage_returns_none_when_missing_token_counts(self) -> None:
        assert _build_usage(None) is None
        assert _build_usage(NS(prompt_tokens=None, completion_tokens=5)) is None
        assert _build_usage(NS(prompt_tokens=10, completion_tokens=None)) is None

    def test_finish_reason_length_maps_max_tokens(self) -> None:
        state = _StreamState()
        state.stream_started = True
        chunk = NS(
            id="x",
            model="m",
            choices=[
                NS(
                    index=0,
                    delta=NS(role=None, content=None, tool_calls=None),
                    finish_reason="length",
                )
            ],
            usage=None,
        )
        out = _translate_chunk(chunk, state)
        assert isinstance(out[-1], Done)
        assert out[-1].stop_reason == "max_tokens"

    def test_finish_reason_content_filter_maps(self) -> None:
        state = _StreamState()
        state.stream_started = True
        chunk = NS(
            id="x",
            model="m",
            choices=[
                NS(
                    index=0,
                    delta=NS(role=None, content=None, tool_calls=None),
                    finish_reason="content_filter",
                )
            ],
            usage=None,
        )
        out = _translate_chunk(chunk, state)
        assert isinstance(out[-1], Done)
        assert out[-1].stop_reason == "content_filter"


from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import openai

from primer.model.chat import Error as ChatError
from primer.model.except_ import (
    AuthenticationError,
    ModelNotFoundError,
)


async def _aiter(items: list) -> AsyncIterator:
    for item in items:
        yield item


def _make_openai_error(cls: type, *, status_code: int = 400, code: str | None = None):
    exc = cls.__new__(cls)
    exc.status_code = status_code
    exc.code = code
    exc.message = f"test {cls.__name__}"
    Exception.__init__(exc, exc.message)
    return exc


def _patched_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_instance = MagicMock()
    mock_instance.chat = MagicMock()
    mock_instance.chat.completions = MagicMock()
    mock_instance.chat.completions.create = AsyncMock()
    cls_mock = MagicMock(return_value=mock_instance)
    monkeypatch.setattr("primer.llm.openchat.AsyncOpenAI", cls_mock)
    return mock_instance


def _simple_text_chunk_seq(model: str = "gpt-4o-mini") -> list[Any]:
    return [
        NS(
            id="chatcmpl-1",
            model=model,
            choices=[
                NS(
                    index=0,
                    delta=NS(role="assistant", content=None, tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=None,
        ),
        NS(
            id="chatcmpl-1",
            model=model,
            choices=[
                NS(
                    index=0,
                    delta=NS(role=None, content="hello", tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=None,
        ),
        NS(
            id="chatcmpl-1",
            model=model,
            choices=[
                NS(
                    index=0,
                    delta=NS(role=None, content=None, tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=NS(prompt_tokens=4, completion_tokens=2),
        ),
    ]


class TestStream:
    async def test_unknown_model_raises_model_not_found(self) -> None:
        llm = OpenChatLLM(_make_provider(models=["gpt-4o-mini"]))
        with pytest.raises(ModelNotFoundError, match="not-a-real-model"):
            async for _ in llm.stream(
                model="not-a-real-model",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_full_stream_emits_start_text_usage_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_text_chunk_seq())

        events = [
            ev
            async for ev in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            )
        ]
        kinds = [type(e).__name__ for e in events]
        assert kinds == ["StreamStart", "TextDelta", "Usage", "Done"]

    async def test_request_payload_sets_stream_and_include_usage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_text_chunk_seq())

        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
        ):
            pass

        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        assert kwargs["model"] == "gpt-4o-mini"
        assert kwargs["messages"][0]["role"] == "user"

    async def test_request_payload_omits_optional_keys_when_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_text_chunk_seq())

        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
        ):
            pass

        kwargs = client.chat.completions.create.call_args.kwargs
        for omitted in (
            "temperature", "top_p", "max_tokens", "stop",
            "tools", "tool_choice", "response_format",
        ):
            assert omitted not in kwargs

    async def test_request_payload_includes_tools_when_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_text_chunk_seq())

        tool = Tool(
            id="search",
            description="Search",
            toolset_id="default",
            args_schema={"type": "object", "properties": {}, "required": []},
        )
        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            tools=[tool],
            tool_choice="auto",
        ):
            pass

        kwargs = client.chat.completions.create.call_args.kwargs
        assert len(kwargs["tools"]) == 1
        assert kwargs["tools"][0]["function"]["name"] == "search"
        assert kwargs["tool_choice"] == "auto"

    async def test_request_payload_routes_response_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_text_chunk_seq())

        class Out(PydanticBaseModel):
            value: int

        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            response_format=Out,
        ):
            pass

        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["response_format"]["type"] == "json_schema"
        assert kwargs["response_format"]["json_schema"]["name"] == "Out"

    async def test_extended_kwargs_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_text_chunk_seq())

        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            extended={
                "parallel_tool_calls": False,
                "seed": 42,
                "frobnicate": True,
            },
        ):
            pass

        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["parallel_tool_calls"] is False
        assert kwargs["seed"] == 42
        assert "frobnicate" not in kwargs


class TestExceptionWrapping:
    async def test_pre_stream_auth_error_reraised_as_matrix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.side_effect = _make_openai_error(
            openai.AuthenticationError, status_code=401
        )
        with pytest.raises(AuthenticationError):
            async for _ in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_mid_stream_exception_yields_terminal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)

        async def failing_iter() -> AsyncIterator:
            yield NS(
                id="x",
                model="gpt-4o-mini",
                choices=[
                    NS(
                        index=0,
                        delta=NS(role="assistant", content=None, tool_calls=None),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            raise _make_openai_error(openai.RateLimitError, status_code=429)

        client.chat.completions.create.return_value = failing_iter()
        events = [
            ev
            async for ev in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            )
        ]
        assert isinstance(events[0], StreamStart)
        assert isinstance(events[-1], ChatError)
        assert events[-1].fatal is True
