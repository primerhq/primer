"""Unit tests for the OpenResponses LLM adapter."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import HttpUrl, SecretStr

from matrix.llm.openresponses import OpenResponsesLLM, _POLICY_BY_FLAVOR, _FlavorPolicy
from matrix.model.except_ import ConfigError
from matrix.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenResponsesConfig,
    OpenResponsesFlavor,
)


# ------------------------------------------------------------------------- #
# Test fixtures                                                              #
# ------------------------------------------------------------------------- #


def _make_provider(
    *,
    flavor: OpenResponsesFlavor = OpenResponsesFlavor.OPENAI,
    api_key: str = "sk-test",
    models: list[str] | None = None,
    max_concurrency: int = 4,
) -> LLMProvider:
    return LLMProvider(
        id="openai-default",
        provider=LLMProviderType.OPENRESPONSES,
        models=[
            LLMModel(name=name, context_length=8192)
            for name in (models or ["gpt-4o-mini"])
        ],
        config=OpenResponsesConfig(
            url=HttpUrl("https://api.openai.com/v1/"),
            api_key=SecretStr(api_key),
            flavor=flavor,
        ),
        limits=Limits(max_concurrency=max_concurrency),
    )


# ------------------------------------------------------------------------- #
# TestFlavorPolicy                                                           #
# ------------------------------------------------------------------------- #


class TestFlavorPolicy:
    def test_openai_policy_requires_api_key(self) -> None:
        policy = _POLICY_BY_FLAVOR[OpenResponsesFlavor.OPENAI]
        assert policy.require_api_key is True
        assert policy.drop_encrypted_reasoning is False
        assert policy.expect_reasoning_under_store_true is True

    def test_lmstudio_policy_tolerates_no_key_drops_encrypted_reasoning(self) -> None:
        policy = _POLICY_BY_FLAVOR[OpenResponsesFlavor.LMSTUDIO]
        assert policy.require_api_key is False
        assert policy.drop_encrypted_reasoning is True
        assert policy.expect_reasoning_under_store_true is False

    def test_other_policy_matches_openai(self) -> None:
        policy = _POLICY_BY_FLAVOR[OpenResponsesFlavor.OTHER]
        assert policy.require_api_key is True
        assert policy.drop_encrypted_reasoning is False
        assert policy.expect_reasoning_under_store_true is True

    def test_policy_dataclass_is_frozen(self) -> None:
        policy = _POLICY_BY_FLAVOR[OpenResponsesFlavor.OPENAI]
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            policy.require_api_key = False  # type: ignore[misc]


# ------------------------------------------------------------------------- #
# TestConstructor                                                            #
# ------------------------------------------------------------------------- #


class TestConstructor:
    def test_accepts_valid_openai_config(self) -> None:
        provider = _make_provider(flavor=OpenResponsesFlavor.OPENAI)
        llm = OpenResponsesLLM(provider)
        assert llm._policy is _POLICY_BY_FLAVOR[OpenResponsesFlavor.OPENAI]
        assert llm._client is None  # lazy

    def test_accepts_valid_lmstudio_config_with_empty_key(self) -> None:
        provider = _make_provider(
            flavor=OpenResponsesFlavor.LMSTUDIO, api_key=""
        )
        llm = OpenResponsesLLM(provider)
        assert llm._policy.require_api_key is False

    def test_rejects_empty_api_key_for_openai_flavor(self) -> None:
        provider = _make_provider(flavor=OpenResponsesFlavor.OPENAI, api_key="")
        with pytest.raises(ConfigError, match="api_key is required"):
            OpenResponsesLLM(provider)

    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        # Tamper with the validated provider for the test.
        object.__setattr__(provider, "provider", "embedding")  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="OPENRESPONSES"):
            OpenResponsesLLM(provider)

    def test_initialises_semaphore_to_max_concurrency(self) -> None:
        provider = _make_provider(max_concurrency=3)
        llm = OpenResponsesLLM(provider)
        # Semaphore exposes its current value via private attr; check by acquiring.
        assert isinstance(llm._semaphore, asyncio.Semaphore)
        # The Semaphore's internal value tracks remaining permits.
        assert llm._semaphore._value == 3  # type: ignore[attr-defined]

    def test_logs_init_with_structured_context(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="matrix.llm.openresponses")
        provider = _make_provider(
            models=["gpt-4o-mini", "gpt-4o"], max_concurrency=2
        )
        OpenResponsesLLM(provider)
        records = [r for r in caplog.records if "OpenResponses adapter" in r.message]
        assert len(records) == 1
        record = records[0]
        assert record.provider_id == "openai-default"  # type: ignore[attr-defined]
        assert record.flavor == "openai"  # type: ignore[attr-defined]
        assert record.models == ["gpt-4o-mini", "gpt-4o"]  # type: ignore[attr-defined]
        assert record.max_concurrency == 2  # type: ignore[attr-defined]


# ------------------------------------------------------------------------- #
# TestListModels                                                             #
# ------------------------------------------------------------------------- #


class TestListModels:
    async def test_returns_configured_model_names(self) -> None:
        provider = _make_provider(models=["gpt-4o-mini", "gpt-4o"])
        llm = OpenResponsesLLM(provider)
        models = list(await llm.list_models())
        assert models == ["gpt-4o-mini", "gpt-4o"]

    async def test_does_not_call_upstream(self) -> None:
        provider = _make_provider()
        llm = OpenResponsesLLM(provider)
        with patch.object(OpenResponsesLLM, "_get_client") as mock_get_client:
            await llm.list_models()
            mock_get_client.assert_not_called()


import openai

from matrix.model.except_ import AuthenticationError


def _make_openai_error(cls: type, *, status_code: int = 400, code: str | None = None):
    """Build an openai SDK exception with minimal init plumbing.

    The SDK's exception constructors require a Response and body in
    real use; for tests we bypass __init__ and set the relevant
    attributes directly.
    """
    exc = cls.__new__(cls)
    exc.status_code = status_code
    exc.code = code
    exc.message = f"test {cls.__name__}"
    Exception.__init__(exc, exc.message)
    return exc


import base64

from matrix.llm.openresponses import _messages_to_input_items, _part_to_input_content
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
from matrix.model.except_ import UnsupportedContentError


class TestPartToInputContent:
    def test_text_part(self) -> None:
        part = TextPart(text="hello")
        assert _part_to_input_content(part) == {"type": "input_text", "text": "hello"}

    def test_image_part_data(self) -> None:
        part = ImagePart(data=b"\x89PNG", mime_type="image/png", detail="high")
        out = _part_to_input_content(part)
        assert out["type"] == "input_image"
        assert out["image_url"].startswith("data:image/png;base64,")
        assert base64.b64decode(out["image_url"].split(",", 1)[1]) == b"\x89PNG"
        assert out["detail"] == "high"

    def test_image_part_data_omits_detail_when_none(self) -> None:
        part = ImagePart(data=b"\x89PNG", mime_type="image/png")
        out = _part_to_input_content(part)
        assert "detail" not in out

    def test_image_part_data_default_mime(self) -> None:
        part = ImagePart(data=b"raw")
        out = _part_to_input_content(part)
        assert out["image_url"].startswith("data:application/octet-stream;base64,")

    def test_image_part_url(self) -> None:
        part = ImagePart(url="https://example.com/img.png", detail="low")
        assert _part_to_input_content(part) == {
            "type": "input_image",
            "image_url": "https://example.com/img.png",
            "detail": "low",
        }

    def test_image_part_file_id(self) -> None:
        part = ImagePart(file_id="file-abc", detail="auto")
        assert _part_to_input_content(part) == {
            "type": "input_image",
            "file_id": "file-abc",
            "detail": "auto",
        }

    def test_document_part_data(self) -> None:
        part = DocumentPart(
            data=b"%PDF-1.4", mime_type="application/pdf", filename="report.pdf"
        )
        out = _part_to_input_content(part)
        assert out["type"] == "input_file"
        assert base64.b64decode(out["file_data"]) == b"%PDF-1.4"
        assert out["filename"] == "report.pdf"

    def test_document_part_data_default_filename(self) -> None:
        part = DocumentPart(data=b"%PDF", mime_type="application/pdf")
        out = _part_to_input_content(part)
        assert out["filename"] == "file"

    def test_document_part_url(self) -> None:
        part = DocumentPart(url="https://example.com/doc.pdf", filename="doc.pdf")
        out = _part_to_input_content(part)
        assert out == {
            "type": "input_file",
            "file_url": "https://example.com/doc.pdf",
            "filename": "doc.pdf",
        }

    def test_document_part_url_omits_filename_when_none(self) -> None:
        part = DocumentPart(url="https://example.com/doc.pdf")
        out = _part_to_input_content(part)
        assert "filename" not in out

    def test_document_part_file_id(self) -> None:
        part = DocumentPart(file_id="file-xyz", filename="ref.pdf")
        out = _part_to_input_content(part)
        assert out == {"type": "input_file", "file_id": "file-xyz", "filename": "ref.pdf"}

    def test_audio_part_mp3(self) -> None:
        part = ExtendedPart(extended=AudioPart(data=b"raw-mp3", mime_type="audio/mp3"))
        out = _part_to_input_content(part)
        assert out["type"] == "input_audio"
        assert out["input_audio"]["format"] == "mp3"
        assert base64.b64decode(out["input_audio"]["data"]) == b"raw-mp3"

    def test_audio_part_mpeg_alias_maps_to_mp3(self) -> None:
        part = ExtendedPart(extended=AudioPart(data=b"raw", mime_type="audio/mpeg"))
        out = _part_to_input_content(part)
        assert out["input_audio"]["format"] == "mp3"

    def test_audio_part_wav(self) -> None:
        part = ExtendedPart(extended=AudioPart(data=b"riff", mime_type="audio/wav"))
        out = _part_to_input_content(part)
        assert out["input_audio"]["format"] == "wav"

    def test_audio_part_unsupported_mime_raises(self) -> None:
        part = ExtendedPart(extended=AudioPart(data=b"raw", mime_type="audio/ogg"))
        with pytest.raises(UnsupportedContentError, match="audio/ogg"):
            _part_to_input_content(part)

    def test_audio_part_url_only_raises(self) -> None:
        part = ExtendedPart(extended=AudioPart(url="https://example.com/a.mp3"))
        with pytest.raises(UnsupportedContentError, match="inline base64"):
            _part_to_input_content(part)

    def test_video_part_raises(self) -> None:
        part = ExtendedPart(extended=VideoPart(url="https://example.com/v.mp4"))
        with pytest.raises(UnsupportedContentError, match="video"):
            _part_to_input_content(part)


class TestMessagesToInputItems:
    def test_simple_user_message(self) -> None:
        items = _messages_to_input_items(
            [Message(role="user", parts=[TextPart(text="hi")])]
        )
        assert items == [
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
        ]

    def test_system_message_inlined(self) -> None:
        items = _messages_to_input_items(
            [
                Message(role="system", parts=[TextPart(text="be terse")]),
                Message(role="user", parts=[TextPart(text="hello")]),
            ]
        )
        assert items[0]["role"] == "system"
        assert items[1]["role"] == "user"

    def test_assistant_message_with_text(self) -> None:
        items = _messages_to_input_items(
            [Message(role="assistant", parts=[TextPart(text="ok")])]
        )
        assert items[0]["role"] == "assistant"

    def test_tool_role_message_flattens_to_function_call_outputs(self) -> None:
        items = _messages_to_input_items(
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
        assert items == [
            {"type": "function_call_output", "call_id": "call_1", "output": "42"},
            {"type": "function_call_output", "call_id": "call_2", "output": "done"},
        ]

    def test_tool_role_with_non_tool_result_raises(self) -> None:
        # Build via construct to bypass schema validation that might block this.
        msg = Message.model_construct(
            role="tool", parts=[TextPart(text="oops")]
        )
        with pytest.raises(UnsupportedContentError, match="ToolResultPart"):
            _messages_to_input_items([msg])

    def test_assistant_message_text_then_tool_call_splits_into_two_items(self) -> None:
        items = _messages_to_input_items(
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
        assert len(items) == 2
        assert items[0] == {
            "role": "assistant",
            "content": [{"type": "input_text", "text": "let me check"}],
        }
        assert items[1] == {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q": "weather"}',
        }

    def test_assistant_message_tool_call_then_text_emits_call_then_new_message(self) -> None:
        items = _messages_to_input_items(
            [
                Message(
                    role="assistant",
                    parts=[
                        ToolCallPart(id="call_1", name="search", arguments={}),
                        TextPart(text="done"),
                    ],
                )
            ]
        )
        assert items[0]["type"] == "function_call"
        assert items[1] == {
            "role": "assistant",
            "content": [{"type": "input_text", "text": "done"}],
        }

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
                role="tool",
                parts=[ToolResultPart(id="call_1", output="sunny")],
            ),
            Message(role="assistant", parts=[TextPart(text="It's sunny.")]),
        ]
        items = _messages_to_input_items(messages)
        roles_or_types = [item.get("role") or item.get("type") for item in items]
        assert roles_or_types == [
            "system",
            "user",
            "function_call",
            "function_call_output",
            "assistant",
        ]


from pydantic import BaseModel as PydanticBaseModel

from matrix.llm.openresponses import (
    _build_sampling_params,
    _extract_extended_kwargs,
    _response_format_to_text_param,
    _tool_choice_to_openai,
    _tool_to_openai,
)
from matrix.model.chat import Tool


class TestTools:
    def test_tool_to_openai(self) -> None:
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
        out = _tool_to_openai(tool)
        assert out == {
            "type": "function",
            "name": "get_weather",
            "description": "Get the weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
        # toolset_id is intentionally NOT transmitted
        assert "toolset_id" not in out


class TestToolChoice:
    def test_none_returns_none_marker(self) -> None:
        assert _tool_choice_to_openai(None) is None

    @pytest.mark.parametrize("mode", ["auto", "required", "none"])
    def test_mode_strings_pass_through(self, mode: str) -> None:
        assert _tool_choice_to_openai(mode) == mode

    def test_specific_tool_name_wraps_to_dict(self) -> None:
        assert _tool_choice_to_openai("get_weather") == {
            "type": "function",
            "name": "get_weather",
        }


class TestResponseFormat:
    def test_none_returns_none(self) -> None:
        assert _response_format_to_text_param(None) is None

    def test_dict_schema(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        out = _response_format_to_text_param(schema)
        assert out == {
            "format": {
                "type": "json_schema",
                "name": "schema",
                "schema": schema,
                "strict": True,
            }
        }

    def test_pydantic_class(self) -> None:
        class Answer(PydanticBaseModel):
            value: int

        out = _response_format_to_text_param(Answer)
        assert out["format"]["name"] == "Answer"  # type: ignore[index]
        assert out["format"]["type"] == "json_schema"  # type: ignore[index]
        assert "value" in out["format"]["schema"]["properties"]  # type: ignore[index]

    def test_invalid_type_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="response_format"):
            _response_format_to_text_param(42)  # type: ignore[arg-type]


class TestSampling:
    def test_all_params_forwarded(self) -> None:
        params = _build_sampling_params(
            temperature=0.7,
            top_p=0.9,
            max_output_tokens=500,
            stop=None,
        )
        assert params == {
            "temperature": 0.7,
            "top_p": 0.9,
            "max_output_tokens": 500,
        }

    def test_all_none_returns_empty_dict(self) -> None:
        assert _build_sampling_params(
            temperature=None, top_p=None, max_output_tokens=None, stop=None
        ) == {}

    def test_stop_silently_dropped_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="matrix.llm.openresponses")
        out = _build_sampling_params(
            temperature=None, top_p=None, max_output_tokens=None, stop=["\n", "END"]
        )
        assert "stop" not in out
        records = [r for r in caplog.records if "stop" in r.message.lower()]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING


class TestExtendedKwargs:
    def test_unknown_keys_dropped_with_debug_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="matrix.llm.openresponses")
        out = _extract_extended_kwargs({"frobnicate": True, "foobar": 42})
        assert out == {}
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any(
            "frobnicate" in r.message and "foobar" in r.message
            for r in debug_records
        )

    def test_reasoning_effort_folds_into_reasoning_dict(self) -> None:
        out = _extract_extended_kwargs({"reasoning_effort": "high"})
        assert out == {"reasoning": {"effort": "high"}}

    def test_reasoning_summary_folds_into_reasoning_dict(self) -> None:
        out = _extract_extended_kwargs({"reasoning_summary": "concise"})
        assert out == {"reasoning": {"summary": "concise"}}

    def test_reasoning_effort_and_summary_merge(self) -> None:
        out = _extract_extended_kwargs(
            {"reasoning_effort": "low", "reasoning_summary": "auto"}
        )
        assert out == {"reasoning": {"effort": "low", "summary": "auto"}}

    @pytest.mark.parametrize(
        "key, value",
        [
            ("parallel_tool_calls", False),
            ("prompt_cache_key", "session-abc"),
            ("service_tier", "flex"),
            ("metadata", {"trace_id": "x"}),
            ("max_tool_calls", 5),
            ("top_logprobs", 3),
        ],
    )
    def test_recognised_keys_passthrough(self, key: str, value: Any) -> None:
        out = _extract_extended_kwargs({key: value})
        assert out == {key: value}

    def test_none_input_returns_empty(self) -> None:
        assert _extract_extended_kwargs(None) == {}

    def test_empty_dict_returns_empty(self) -> None:
        assert _extract_extended_kwargs({}) == {}


from matrix.llm.openresponses import (
    _StreamState,
    _map_incomplete_reason,
    _map_stop_reason,
    _translate_event,
)


class TestStopReason:
    def test_completed_no_function_call_maps_to_stop(self) -> None:
        state = _StreamState()
        assert _map_stop_reason("completed", state) == "stop"

    def test_completed_with_function_call_maps_to_tool_use(self) -> None:
        state = _StreamState()
        state.saw_function_call = True
        assert _map_stop_reason("completed", state) == "tool_use"

    def test_failed_maps_to_error(self) -> None:
        assert _map_stop_reason("failed", _StreamState()) == "error"

    def test_unknown_status_maps_to_other(self) -> None:
        assert _map_stop_reason("queued", _StreamState()) == "other"

    def test_max_output_tokens_maps_to_max_tokens(self) -> None:
        assert _map_incomplete_reason("max_output_tokens") == "max_tokens"

    def test_content_filter_maps_to_content_filter(self) -> None:
        assert _map_incomplete_reason("content_filter") == "content_filter"

    def test_unknown_incomplete_maps_to_other(self) -> None:
        assert _map_incomplete_reason("weird") == "other"

    def test_none_incomplete_maps_to_other(self) -> None:
        assert _map_incomplete_reason(None) == "other"


from types import SimpleNamespace as NS

from matrix.model.chat import (
    Citation,
    Done,
    ExtendedEvent,
    MediaDelta,
    RawReasoningDelta,
    ReasoningDelta,
    RefusalDelta,
    ServerToolCallDelta,
    ServerToolCallEnd,
    ServerToolCallStart,
    StreamStart,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from matrix.model.chat import Error as ChatError


class TestStreamMapping:
    def test_response_created_emits_stream_start(self) -> None:
        ev = NS(type="response.created", response=NS(id="resp_1", model="gpt-4o"))
        out = _translate_event(ev, _StreamState())
        assert len(out) == 1
        assert isinstance(out[0], StreamStart)
        assert out[0].request_id == "resp_1"
        assert out[0].model == "gpt-4o"

    def test_message_item_added_registers_index_silently(self) -> None:
        state = _StreamState()
        ev = NS(
            type="response.output_item.added",
            item=NS(type="message", id="msg_1"),
        )
        assert _translate_event(ev, state) == []
        assert state.block_index[("msg_1", None)] == 0
        assert state.next_index == 1

    def test_function_call_item_emits_tool_call_start_and_sets_flag(self) -> None:
        state = _StreamState()
        ev = NS(
            type="response.output_item.added",
            item=NS(
                type="function_call", id="item_1", call_id="call_1", name="search"
            ),
        )
        out = _translate_event(ev, state)
        assert len(out) == 1
        assert isinstance(out[0], ToolCallStart)
        assert out[0].id == "call_1"
        assert out[0].name == "search"
        assert state.saw_function_call is True

    def test_server_tool_item_emits_extended_server_tool_call_start(self) -> None:
        state = _StreamState()
        ev = NS(
            type="response.output_item.added",
            item=NS(type="web_search_call", id="ws_1"),
        )
        out = _translate_event(ev, state)
        assert len(out) == 1
        assert isinstance(out[0], ExtendedEvent)
        assert isinstance(out[0].extended, ServerToolCallStart)
        assert out[0].extended.tool_name == "web_search"

    def test_text_delta(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(type="response.output_item.added", item=NS(type="message", id="msg_1")),
            state,
        )
        _translate_event(
            NS(type="response.content_part.added", item_id="msg_1", content_index=0),
            state,
        )
        out = _translate_event(
            NS(
                type="response.output_text.delta",
                item_id="msg_1",
                content_index=0,
                delta="hi",
            ),
            state,
        )
        assert len(out) == 1
        assert isinstance(out[0], TextDelta)
        assert out[0].text == "hi"

    def test_reasoning_summary_delta_maps_to_reasoning_delta(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(
                type="response.output_item.added",
                item=NS(type="reasoning", id="r_1"),
            ),
            state,
        )
        out = _translate_event(
            NS(
                type="response.reasoning_summary_text.delta",
                item_id="r_1",
                summary_index=0,
                delta="thinking",
            ),
            state,
        )
        assert isinstance(out[0], ReasoningDelta)
        assert out[0].text == "thinking"

    def test_reasoning_text_delta_maps_to_extended_raw_reasoning(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(
                type="response.reasoning_text.delta",
                item_id="r_1",
                content_index=0,
                delta="raw",
            ),
            state,
        )
        assert isinstance(out[0], ExtendedEvent)
        assert isinstance(out[0].extended, RawReasoningDelta)

    def test_refusal_delta_maps_to_extended_refusal(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(
                type="response.refusal.delta",
                item_id="msg_1",
                content_index=0,
                delta="cannot",
            ),
            state,
        )
        assert isinstance(out[0].extended, RefusalDelta)  # type: ignore[union-attr]

    def test_function_call_arguments_delta(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(
                type="response.output_item.added",
                item=NS(
                    type="function_call",
                    id="item_1",
                    call_id="call_1",
                    name="search",
                ),
            ),
            state,
        )
        out = _translate_event(
            NS(
                type="response.function_call_arguments.delta",
                item_id="item_1",
                delta='{"q":',
            ),
            state,
        )
        assert isinstance(out[0], ToolCallDelta)
        assert out[0].arguments_delta == '{"q":'
        assert out[0].id == "call_1"

    def test_function_call_arguments_done_parses_json(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(
                type="response.output_item.added",
                item=NS(
                    type="function_call",
                    id="item_1",
                    call_id="call_1",
                    name="search",
                ),
            ),
            state,
        )
        out = _translate_event(
            NS(
                type="response.function_call_arguments.done",
                item_id="item_1",
                arguments='{"q": "weather"}',
            ),
            state,
        )
        assert isinstance(out[0], ToolCallEnd)
        assert out[0].arguments == {"q": "weather"}

    def test_function_call_arguments_done_handles_invalid_json(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(
                type="response.output_item.added",
                item=NS(
                    type="function_call",
                    id="item_1",
                    call_id="call_1",
                    name="search",
                ),
            ),
            state,
        )
        out = _translate_event(
            NS(
                type="response.function_call_arguments.done",
                item_id="item_1",
                arguments="not json",
            ),
            state,
        )
        assert isinstance(out[0], ToolCallEnd)
        assert out[0].arguments == {}

    def test_audio_delta(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(
                type="response.audio.delta",
                item_id="msg_1",
                content_index=0,
                delta=base64.b64encode(b"audio").decode(),
            ),
            state,
        )
        assert isinstance(out[0], MediaDelta)
        assert out[0].kind == "audio"
        assert out[0].data == b"audio"

    def test_image_partial(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(
                type="response.image_generation_call.partial_image",
                item_id="img_1",
                partial_image_b64=base64.b64encode(b"png").decode(),
            ),
            state,
        )
        assert isinstance(out[0], MediaDelta)
        assert out[0].kind == "image"

    def test_code_interpreter_code_delta(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(
                type="response.code_interpreter_call.code.delta",
                item_id="ci_1",
                delta="print(1)",
            ),
            state,
        )
        assert isinstance(out[0].extended, ServerToolCallDelta)  # type: ignore[union-attr]

    def test_server_tool_completion_emits_server_tool_call_end(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(
                type="response.output_item.added",
                item=NS(type="web_search_call", id="ws_1"),
            ),
            state,
        )
        out = _translate_event(
            NS(type="response.web_search_call.completed", item_id="ws_1"), state
        )
        assert len(out) == 1
        assert isinstance(out[0].extended, ServerToolCallEnd)  # type: ignore[union-attr]

    def test_annotation_added_emits_citation(self) -> None:
        state = _StreamState()
        annotation = NS(
            url="https://example.com",
            title="Example",
            file_id=None,
            container_id=None,
            quote=None,
            text=None,
            start_index=0,
            end_index=10,
        )
        out = _translate_event(
            NS(
                type="response.output_text_annotation.added",
                item_id="msg_1",
                content_index=0,
                annotation=annotation,
            ),
            state,
        )
        assert isinstance(out[0].extended, Citation)  # type: ignore[union-attr]
        assert out[0].extended.source_url == "https://example.com"  # type: ignore[union-attr]

    def test_response_completed_emits_usage_then_done(self) -> None:
        state = _StreamState()
        usage = NS(
            input_tokens=100,
            output_tokens=50,
            input_tokens_details=NS(cached_tokens=10),
            output_tokens_details=NS(reasoning_tokens=20),
        )
        ev = NS(
            type="response.completed",
            response=NS(usage=usage),
        )
        out = _translate_event(ev, state)
        assert len(out) == 2
        assert isinstance(out[0], Usage)
        assert out[0].input_tokens == 100
        assert out[0].cached_input_tokens == 10
        assert out[0].reasoning_tokens == 20
        assert out[0].cumulative is False
        assert isinstance(out[1], Done)
        assert out[1].stop_reason == "stop"
        assert out[1].raw_reason == "completed"

    def test_response_completed_no_usage_emits_only_done(self) -> None:
        state = _StreamState()
        ev = NS(type="response.completed", response=NS(usage=None))
        out = _translate_event(ev, state)
        assert len(out) == 1
        assert isinstance(out[0], Done)

    def test_response_failed_emits_done_with_error(self) -> None:
        out = _translate_event(NS(type="response.failed"), _StreamState())
        assert isinstance(out[0], Done)
        assert out[0].stop_reason == "error"

    def test_response_incomplete_max_tokens(self) -> None:
        ev = NS(
            type="response.incomplete",
            response=NS(incomplete_details=NS(reason="max_output_tokens")),
        )
        out = _translate_event(ev, _StreamState())
        assert isinstance(out[0], Done)
        assert out[0].stop_reason == "max_tokens"
        assert out[0].raw_reason == "incomplete:max_output_tokens"

    def test_error_event_emits_non_fatal_error(self) -> None:
        out = _translate_event(
            NS(type="error", code="rate_limited", message="slow down"),
            _StreamState(),
        )
        assert isinstance(out[0], ChatError)
        assert out[0].fatal is False
        assert out[0].code == "rate_limited"

    def test_unknown_event_type_returns_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="matrix.llm.openresponses")
        out = _translate_event(NS(type="response.some_future_event"), _StreamState())
        assert out == []
        assert any("response.some_future_event" in r.message for r in caplog.records)


from collections.abc import AsyncIterator

from matrix.model.except_ import ModelNotFoundError


async def _aiter(items: list) -> AsyncIterator:
    for item in items:
        yield item


def _patched_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the AsyncOpenAI symbol in the adapter module to a MagicMock.

    Returns the mock instance the adapter will see when it constructs
    the client. Tests configure ``mock.responses.create`` to drive the
    SDK behaviour.
    """
    mock_instance = MagicMock()
    mock_instance.responses = MagicMock()
    mock_instance.responses.create = AsyncMock()
    cls_mock = MagicMock(return_value=mock_instance)
    monkeypatch.setattr("matrix.llm.openresponses.AsyncOpenAI", cls_mock)
    return mock_instance


class TestStream:
    async def test_unknown_model_raises_model_not_found(self) -> None:
        provider = _make_provider(models=["gpt-4o-mini"])
        llm = OpenResponsesLLM(provider)
        with pytest.raises(ModelNotFoundError, match="not-a-real-model"):
            async for _ in llm.stream(
                model="not-a-real-model",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_full_stream_emits_start_text_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = OpenResponsesLLM(provider)
        client = _patched_client(monkeypatch)
        events = [
            NS(
                type="response.created",
                response=NS(id="resp_1", model="gpt-4o-mini"),
            ),
            NS(
                type="response.output_item.added",
                item=NS(type="message", id="msg_1"),
            ),
            NS(
                type="response.content_part.added", item_id="msg_1", content_index=0
            ),
            NS(
                type="response.output_text.delta",
                item_id="msg_1",
                content_index=0,
                delta="hi",
            ),
            NS(type="response.completed", response=NS(usage=None)),
        ]
        client.responses.create.return_value = _aiter(events)

        out = [
            ev
            async for ev in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hello")])],
            )
        ]
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["StreamStart", "TextDelta", "Done"]

    async def test_request_payload_has_store_false_and_stream_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = OpenResponsesLLM(provider)
        client = _patched_client(monkeypatch)
        client.responses.create.return_value = _aiter(
            [NS(type="response.completed", response=NS(usage=None))]
        )
        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
        ):
            pass
        kwargs = client.responses.create.call_args.kwargs
        assert kwargs["store"] is False
        assert kwargs["stream"] is True
        assert kwargs["model"] == "gpt-4o-mini"
        assert kwargs["input"][0]["role"] == "user"

    async def test_request_payload_includes_tools_when_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = OpenResponsesLLM(provider)
        client = _patched_client(monkeypatch)
        client.responses.create.return_value = _aiter(
            [NS(type="response.completed", response=NS(usage=None))]
        )
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
        kwargs = client.responses.create.call_args.kwargs
        assert len(kwargs["tools"]) == 1
        assert kwargs["tools"][0]["name"] == "search"
        assert kwargs["tool_choice"] == "auto"

    async def test_request_payload_omits_optional_keys_when_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = OpenResponsesLLM(provider)
        client = _patched_client(monkeypatch)
        client.responses.create.return_value = _aiter(
            [NS(type="response.completed", response=NS(usage=None))]
        )
        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
        ):
            pass
        kwargs = client.responses.create.call_args.kwargs
        for omitted in ("temperature", "top_p", "max_output_tokens", "tools",
                        "tool_choice", "text", "reasoning"):
            assert omitted not in kwargs

    async def test_response_format_routes_to_text_param(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = OpenResponsesLLM(provider)
        client = _patched_client(monkeypatch)
        client.responses.create.return_value = _aiter(
            [NS(type="response.completed", response=NS(usage=None))]
        )

        class Out(PydanticBaseModel):
            value: int

        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            response_format=Out,
        ):
            pass
        kwargs = client.responses.create.call_args.kwargs
        assert kwargs["text"]["format"]["type"] == "json_schema"

    async def test_extended_kwargs_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = OpenResponsesLLM(provider)
        client = _patched_client(monkeypatch)
        client.responses.create.return_value = _aiter(
            [NS(type="response.completed", response=NS(usage=None))]
        )
        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            extended={
                "reasoning_effort": "high",
                "parallel_tool_calls": False,
                "frobnicate": True,
            },
        ):
            pass
        kwargs = client.responses.create.call_args.kwargs
        assert kwargs["reasoning"] == {"effort": "high"}
        assert kwargs["parallel_tool_calls"] is False
        assert "frobnicate" not in kwargs


class TestExceptionWrapping:
    async def test_pre_stream_exception_re_raised_as_matrix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = OpenResponsesLLM(provider)
        client = _patched_client(monkeypatch)
        client.responses.create.side_effect = _make_openai_error(
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
        provider = _make_provider()
        llm = OpenResponsesLLM(provider)
        client = _patched_client(monkeypatch)

        async def failing_iter() -> AsyncIterator:
            yield NS(
                type="response.created",
                response=NS(id="resp_1", model="gpt-4o-mini"),
            )
            raise _make_openai_error(openai.RateLimitError, status_code=429)

        client.responses.create.return_value = failing_iter()
        events = [
            ev
            async for ev in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
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
        llm = OpenResponsesLLM(provider)
        client = _patched_client(monkeypatch)

        in_flight = 0
        peak = 0

        async def slow_iter() -> AsyncIterator:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            yield NS(type="response.completed", response=NS(usage=None))
            in_flight -= 1

        client.responses.create.side_effect = lambda **_: slow_iter()

        async def consume() -> None:
            async for _ in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

        await asyncio.gather(consume(), consume(), consume())
        assert peak == 1


class TestPackageReexport:
    def test_openresponses_llm_reexported_from_package(self) -> None:
        import matrix.llm as llm_pkg

        assert "OpenResponsesLLM" in llm_pkg.__all__
        assert llm_pkg.OpenResponsesLLM is OpenResponsesLLM
