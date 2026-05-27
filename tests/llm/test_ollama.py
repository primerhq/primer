"""Unit tests for the Ollama LLM adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import ollama
import pytest
from pydantic import BaseModel as PydanticBaseModel
from pydantic import HttpUrl, SecretStr

from matrix.llm.ollama import (
    OllamaLLM,
    _build_options_and_kwargs,
    _classify_ollama_exception,
    _OPTIONS_KEYS,
    _StreamState,
    _TOP_LEVEL_KEYS,
    _map_stop_reason,
    _maybe_log_unsupported_tool_choice,
    _messages_to_ollama,
    _next_index,
    _response_format_to_ollama,
    _tools_to_ollama,
    _translate_chunk,
)
from matrix.model.chat import (
    AudioPart,
    DocumentPart,
    Done,
    ExtendedPart,
    ImagePart,
    Message,
    ReasoningDelta,
    StreamStart,
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
from matrix.model.chat import Error as ChatError
from matrix.model.except_ import (
    AuthenticationError,
    BadRequestError,
    ConfigError,
    ModelNotFoundError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
    UnsupportedContentError,
)
from matrix.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OllamaConfig,
)


# ============================================================================
# Helpers
# ============================================================================


def _make_provider(
    *,
    url: str = "http://localhost:11434",
    api_key: str | None = None,
    models: list[str] | None = None,
    max_concurrency: int = 4,
) -> LLMProvider:
    config = OllamaConfig(
        url=HttpUrl(url),
        api_key=SecretStr(api_key) if api_key is not None else None,
    )
    return LLMProvider(
        id="ollama-default",
        provider=LLMProviderType.OLLAMA,
        models=[
            LLMModel(name=name, context_length=8192)
            for name in (models or ["llama3"])
        ],
        config=config,
        limits=Limits(max_concurrency=max_concurrency),
    )


async def _aiter(items: list) -> AsyncIterator:
    for item in items:
        yield item


def _patched_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the ollama.AsyncClient symbol in the adapter module to a MagicMock.

    Returns the mock instance the adapter will see when it constructs
    the client. Tests configure ``mock.chat`` to drive the SDK behaviour.
    """
    mock_instance = MagicMock()
    mock_instance.chat = AsyncMock()
    cls_mock = MagicMock(return_value=mock_instance)
    monkeypatch.setattr("matrix.llm.ollama.ollama.AsyncClient", cls_mock)
    return mock_instance


def _ok_chunks() -> list[Any]:
    """A minimal valid Ollama stream — one text chunk, then done."""
    return [
        NS(
            model="llama3",
            done=False,
            message=NS(content="hi", thinking=None, tool_calls=None),
        ),
        NS(
            model="llama3",
            done=True,
            done_reason="stop",
            prompt_eval_count=5,
            eval_count=3,
            message=NS(content="", thinking=None, tool_calls=None),
        ),
    ]


def _make_response_error(status_code: int, msg: str = "boom") -> ollama.ResponseError:
    """Build an ollama.ResponseError with a status code."""
    exc = ollama.ResponseError(msg)
    exc.status_code = status_code
    return exc


# ============================================================================
# TestConstructor
# ============================================================================


class TestConstructor:
    def test_accepts_valid_config(self) -> None:
        provider = _make_provider()
        llm = OllamaLLM(provider)
        assert llm._client is None

    def test_accepts_with_api_key(self) -> None:
        provider = _make_provider(api_key="bearer-token")
        llm = OllamaLLM(provider)
        assert llm._config.api_key is not None
        assert llm._config.api_key.get_secret_value() == "bearer-token"

    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", "openresponses")  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="OLLAMA"):
            OllamaLLM(provider)

    def test_rejects_wrong_config_type(self) -> None:
        from matrix.model.provider import OpenResponsesConfig

        provider = LLMProvider(
            id="x",
            provider=LLMProviderType.OLLAMA,
            models=[LLMModel(name="llama3", context_length=8192)],
            config=OpenResponsesConfig(  # type: ignore[arg-type]
                url=HttpUrl("https://x/v1/"),
                api_key=SecretStr("sk-x"),
            ),
            limits=Limits(max_concurrency=1),
        )
        with pytest.raises(ConfigError, match="OllamaConfig"):
            OllamaLLM(provider)

    def test_initialises_rate_limiter(self) -> None:
        from matrix.coordinator.in_memory import InMemoryRateLimiter
        provider = _make_provider(max_concurrency=3)
        llm = OllamaLLM(provider)
        assert isinstance(llm._rate_limiter, InMemoryRateLimiter)
        assert llm._max_concurrency == 3

    def test_logs_init(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="matrix.llm.ollama")
        provider = _make_provider(models=["llama3", "mistral"], max_concurrency=2)
        OllamaLLM(provider)
        records = [r for r in caplog.records if "Ollama adapter initialized" in r.message]
        assert len(records) == 1
        assert records[0].provider_id == "ollama-default"  # type: ignore[attr-defined]


# ============================================================================
# TestListModels
# ============================================================================


class TestListModels:
    async def test_returns_configured_names(self) -> None:
        provider = _make_provider(models=["llama3", "mistral"])
        llm = OllamaLLM(provider)
        assert list(await llm.list_models()) == ["llama3", "mistral"]


# ============================================================================
# TestGetClient
# ============================================================================


class TestGetClient:
    def test_lazy_client_no_api_key_no_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cls_mock = MagicMock(return_value=MagicMock())
        monkeypatch.setattr("matrix.llm.ollama.ollama.AsyncClient", cls_mock)
        provider = _make_provider()
        llm = OllamaLLM(provider)
        llm._get_client()
        kwargs = cls_mock.call_args.kwargs
        assert kwargs["headers"] is None
        assert "localhost:11434" in kwargs["host"]

    def test_lazy_client_with_api_key_sets_authorization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cls_mock = MagicMock(return_value=MagicMock())
        monkeypatch.setattr("matrix.llm.ollama.ollama.AsyncClient", cls_mock)
        provider = _make_provider(api_key="my-token")
        llm = OllamaLLM(provider)
        llm._get_client()
        kwargs = cls_mock.call_args.kwargs
        assert kwargs["headers"] == {"Authorization": "Bearer my-token"}

    def test_lazy_client_with_empty_api_key_no_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cls_mock = MagicMock(return_value=MagicMock())
        monkeypatch.setattr("matrix.llm.ollama.ollama.AsyncClient", cls_mock)
        provider = _make_provider(api_key="")
        llm = OllamaLLM(provider)
        llm._get_client()
        kwargs = cls_mock.call_args.kwargs
        assert kwargs["headers"] is None

    def test_client_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cls_mock = MagicMock(return_value=MagicMock())
        monkeypatch.setattr("matrix.llm.ollama.ollama.AsyncClient", cls_mock)
        provider = _make_provider()
        llm = OllamaLLM(provider)
        c1 = llm._get_client()
        c2 = llm._get_client()
        assert c1 is c2
        assert cls_mock.call_count == 1


# ============================================================================
# TestMessagesToOllama
# ============================================================================


class TestMessagesToOllama:
    def test_simple_user_text(self) -> None:
        out = _messages_to_ollama(
            [Message(role="user", parts=[TextPart(text="hi")])]
        )
        assert out == [{"role": "user", "content": "hi"}]

    def test_multiple_text_parts_concatenated(self) -> None:
        out = _messages_to_ollama(
            [
                Message(
                    role="user",
                    parts=[TextPart(text="line1"), TextPart(text="line2")],
                )
            ]
        )
        assert out == [{"role": "user", "content": "line1\nline2"}]

    def test_image_data_appended_to_images(self) -> None:
        out = _messages_to_ollama(
            [
                Message(
                    role="user",
                    parts=[
                        TextPart(text="describe"),
                        ImagePart(data=b"\x89PNG", mime_type="image/png"),
                    ],
                )
            ]
        )
        assert out == [
            {
                "role": "user",
                "content": "describe",
                "images": [b"\x89PNG"],
            }
        ]

    def test_image_url_without_data_raises(self) -> None:
        msg = Message(
            role="user",
            parts=[ImagePart(url="https://example.com/img.png")],
        )
        with pytest.raises(UnsupportedContentError, match="inline image data"):
            _messages_to_ollama([msg])

    def test_document_raises(self) -> None:
        msg = Message(
            role="user",
            parts=[DocumentPart(data=b"%PDF", mime_type="application/pdf")],
        )
        with pytest.raises(UnsupportedContentError, match="documents"):
            _messages_to_ollama([msg])

    def test_audio_raises(self) -> None:
        msg = Message(
            role="user",
            parts=[ExtendedPart(extended=AudioPart(data=b"x", mime_type="audio/mp3"))],
        )
        with pytest.raises(UnsupportedContentError, match="audio"):
            _messages_to_ollama([msg])

    def test_video_raises(self) -> None:
        msg = Message(
            role="user",
            parts=[ExtendedPart(extended=VideoPart(url="https://example.com/v.mp4"))],
        )
        with pytest.raises(UnsupportedContentError, match="video"):
            _messages_to_ollama([msg])

    def test_assistant_with_tool_call(self) -> None:
        out = _messages_to_ollama(
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
        assert out == [
            {
                "role": "assistant",
                "content": "let me check",
                "tool_calls": [
                    {
                        "function": {
                            "name": "search",
                            "arguments": {"q": "weather"},
                        }
                    }
                ],
            }
        ]

    def test_tool_role_uses_id_to_name_lookup(self) -> None:
        out = _messages_to_ollama(
            [
                Message(
                    role="assistant",
                    parts=[
                        ToolCallPart(
                            id="call_1",
                            name="search",
                            arguments={"q": "x"},
                        )
                    ],
                ),
                Message(
                    role="tool",
                    parts=[ToolResultPart(id="call_1", output="42")],
                ),
            ]
        )
        # Last message should carry the looked-up tool_name.
        assert out[-1] == {
            "role": "tool",
            "content": "42",
            "tool_name": "search",
        }

    def test_tool_role_with_unknown_id_uses_empty_name(self) -> None:
        out = _messages_to_ollama(
            [
                Message(
                    role="tool",
                    parts=[ToolResultPart(id="unknown", output="42")],
                )
            ]
        )
        assert out == [
            {"role": "tool", "content": "42", "tool_name": ""}
        ]

    def test_tool_role_with_non_tool_result_raises(self) -> None:
        msg = Message.model_construct(
            role="tool", parts=[TextPart(text="oops")]
        )
        with pytest.raises(UnsupportedContentError, match="tool-role messages"):
            _messages_to_ollama([msg])


# ============================================================================
# TestTools
# ============================================================================


class TestTools:
    def test_none_returns_none(self) -> None:
        assert _tools_to_ollama(None) is None

    def test_empty_list_returns_none(self) -> None:
        assert _tools_to_ollama([]) is None

    def test_single_tool_nested_shape(self) -> None:
        tool = Tool(
            id="get_weather",
            description="Get weather",
            toolset_id="weather_kit",
            args_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        )
        out = _tools_to_ollama([tool])
        assert out == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ]


# ============================================================================
# TestToolChoice
# ============================================================================


class TestToolChoice:
    def test_none_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="matrix.llm.ollama")
        _maybe_log_unsupported_tool_choice(None)
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert all("tool_choice" not in r.message for r in debug_records)

    def test_non_none_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="matrix.llm.ollama")
        _maybe_log_unsupported_tool_choice("auto")
        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "tool_choice" in r.message
        ]
        assert len(debug_records) == 1


# ============================================================================
# TestResponseFormat
# ============================================================================


class TestResponseFormat:
    def test_none_returns_none(self) -> None:
        assert _response_format_to_ollama(None) is None

    def test_dict_passthrough(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        assert _response_format_to_ollama(schema) == schema

    def test_pydantic_class_returns_schema(self) -> None:
        class Answer(PydanticBaseModel):
            value: int

        out = _response_format_to_ollama(Answer)
        assert isinstance(out, dict)
        assert "properties" in out
        assert "value" in out["properties"]

    def test_invalid_type_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="response_format"):
            _response_format_to_ollama(42)  # type: ignore[arg-type]


# ============================================================================
# TestOptionsAndKwargs
# ============================================================================


class TestOptionsAndKwargs:
    def test_all_none_returns_empty(self) -> None:
        options, top = _build_options_and_kwargs(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=None,
            extended=None,
        )
        assert options == {}
        assert top == {}

    def test_all_sampling_to_options(self) -> None:
        options, top = _build_options_and_kwargs(
            temperature=0.7,
            top_p=0.9,
            max_output_tokens=128,
            stop=["END"],
            extended=None,
        )
        assert options == {
            "temperature": 0.7,
            "top_p": 0.9,
            "num_predict": 128,
            "stop": ["END"],
        }
        assert top == {}

    def test_max_output_tokens_renamed_to_num_predict(self) -> None:
        options, _ = _build_options_and_kwargs(
            temperature=None,
            top_p=None,
            max_output_tokens=64,
            stop=None,
            extended=None,
        )
        assert options == {"num_predict": 64}

    @pytest.mark.parametrize(
        "key,value",
        [
            ("top_k", 40),
            ("seed", 42),
            ("repeat_penalty", 1.1),
            ("frequency_penalty", 0.5),
            ("presence_penalty", 0.5),
            ("mirostat", 2),
            ("mirostat_tau", 5.0),
            ("mirostat_eta", 0.1),
            ("tfs_z", 1.0),
            ("typical_p", 1.0),
            ("num_ctx", 4096),
            ("num_batch", 512),
            ("num_gpu", 1),
        ],
    )
    def test_options_keys_routed_to_options(self, key: str, value: Any) -> None:
        options, top = _build_options_and_kwargs(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=None,
            extended={key: value},
        )
        assert options == {key: value}
        assert top == {}

    @pytest.mark.parametrize(
        "key,value",
        [
            ("keep_alive", "5m"),
            ("think", True),
        ],
    )
    def test_top_level_keys_routed_to_top(self, key: str, value: Any) -> None:
        options, top = _build_options_and_kwargs(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=None,
            extended={key: value},
        )
        assert options == {}
        assert top == {key: value}

    def test_unknown_keys_dropped_with_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="matrix.llm.ollama")
        options, top = _build_options_and_kwargs(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=None,
            extended={"frobnicate": True, "wibble": 42},
        )
        assert options == {}
        assert top == {}
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any(
            "frobnicate" in r.message and "wibble" in r.message
            for r in debug_records
        )

    def test_options_keys_frozenset(self) -> None:
        assert isinstance(_OPTIONS_KEYS, frozenset)
        assert "top_k" in _OPTIONS_KEYS

    def test_top_level_keys_frozenset(self) -> None:
        assert isinstance(_TOP_LEVEL_KEYS, frozenset)
        assert "keep_alive" in _TOP_LEVEL_KEYS
        assert "think" in _TOP_LEVEL_KEYS


# ============================================================================
# TestStopReason
# ============================================================================


class TestStopReason:
    def test_stop_no_tool_call(self) -> None:
        state = _StreamState()
        assert _map_stop_reason("stop", state) == "stop"

    def test_stop_with_tool_call(self) -> None:
        state = _StreamState()
        state.saw_tool_call = True
        assert _map_stop_reason("stop", state) == "tool_use"

    def test_length_maps_to_max_tokens(self) -> None:
        state = _StreamState()
        assert _map_stop_reason("length", state) == "max_tokens"

    def test_load_maps_to_other(self) -> None:
        state = _StreamState()
        assert _map_stop_reason("load", state) == "other"

    def test_unknown_maps_to_other(self) -> None:
        state = _StreamState()
        assert _map_stop_reason("something_unknown", state) == "other"

    def test_none_maps_to_other(self) -> None:
        state = _StreamState()
        assert _map_stop_reason(None, state) == "other"


# ============================================================================
# TestNextIndex
# ============================================================================


class TestNextIndex:
    def test_increments_each_call(self) -> None:
        state = _StreamState()
        assert _next_index(state) == 0
        assert _next_index(state) == 1
        assert _next_index(state) == 2
        assert state.next_index == 3


# ============================================================================
# TestStreamMapping
# ============================================================================


class TestStreamMapping:
    def test_first_chunk_emits_stream_start(self) -> None:
        state = _StreamState()
        chunk = NS(
            model="llama3",
            done=False,
            message=NS(content="", thinking=None, tool_calls=None),
        )
        out = _translate_chunk(chunk, state, model_name="llama3")
        assert any(isinstance(e, StreamStart) for e in out)
        assert state.emitted_stream_start is True

    def test_first_chunk_falls_back_to_caller_model(self) -> None:
        state = _StreamState()
        chunk = NS(
            model=None,
            done=False,
            message=NS(content="", thinking=None, tool_calls=None),
        )
        out = _translate_chunk(chunk, state, model_name="caller-model")
        starts = [e for e in out if isinstance(e, StreamStart)]
        assert len(starts) == 1
        assert starts[0].model == "caller-model"

    def test_text_emits_text_delta(self) -> None:
        state = _StreamState()
        chunk = NS(
            model="m",
            done=False,
            message=NS(content="hi", thinking=None, tool_calls=None),
        )
        out = _translate_chunk(chunk, state, model_name="m")
        deltas = [e for e in out if isinstance(e, TextDelta)]
        assert len(deltas) == 1
        assert deltas[0].text == "hi"
        assert deltas[0].index == state.text_index

    def test_thinking_emits_reasoning(self) -> None:
        state = _StreamState()
        chunk = NS(
            model="m",
            done=False,
            message=NS(content=None, thinking="pondering", tool_calls=None),
        )
        out = _translate_chunk(chunk, state, model_name="m")
        deltas = [e for e in out if isinstance(e, ReasoningDelta)]
        assert len(deltas) == 1
        assert deltas[0].text == "pondering"

    def test_tool_calls_atomic_start_delta_end(self) -> None:
        state = _StreamState()
        chunk = NS(
            model="m",
            done=False,
            message=NS(
                content=None,
                thinking=None,
                tool_calls=[
                    NS(
                        function=NS(name="search", arguments={"q": "weather"})
                    )
                ],
            ),
        )
        out = _translate_chunk(chunk, state, model_name="m")
        # StreamStart, ToolCallStart, ToolCallDelta, ToolCallEnd
        kinds = [type(e).__name__ for e in out]
        assert kinds == [
            "StreamStart",
            "ToolCallStart",
            "ToolCallDelta",
            "ToolCallEnd",
        ]
        start = next(e for e in out if isinstance(e, ToolCallStart))
        delta = next(e for e in out if isinstance(e, ToolCallDelta))
        end = next(e for e in out if isinstance(e, ToolCallEnd))
        assert start.id == "call_0"
        assert start.name == "search"
        assert json.loads(delta.arguments_delta) == {"q": "weather"}
        assert end.arguments == {"q": "weather"}
        assert state.saw_tool_call is True

    def test_done_emits_usage_then_done(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        chunk = NS(
            model="m",
            done=True,
            done_reason="stop",
            prompt_eval_count=5,
            eval_count=3,
            message=NS(content="", thinking=None, tool_calls=None),
        )
        out = _translate_chunk(chunk, state, model_name="m")
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["Usage", "Done"]
        usage = out[0]
        assert isinstance(usage, Usage)
        assert usage.input_tokens == 5
        assert usage.output_tokens == 3
        assert usage.cumulative is False
        done = out[1]
        assert isinstance(done, Done)
        assert done.stop_reason == "stop"
        assert done.raw_reason == "stop"

    def test_done_without_token_counts_omits_usage(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        chunk = NS(
            model="m",
            done=True,
            done_reason="stop",
            prompt_eval_count=None,
            eval_count=None,
            message=NS(content="", thinking=None, tool_calls=None),
        )
        out = _translate_chunk(chunk, state, model_name="m")
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["Done"]
        done = out[0]
        assert isinstance(done, Done)
        assert done.raw_reason == "stop"

    def test_done_with_no_reason_uses_unknown(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        chunk = NS(
            model="m",
            done=True,
            done_reason=None,
            prompt_eval_count=None,
            eval_count=None,
            message=NS(content="", thinking=None, tool_calls=None),
        )
        out = _translate_chunk(chunk, state, model_name="m")
        done = out[0]
        assert isinstance(done, Done)
        assert done.raw_reason == "unknown"


# ============================================================================
# TestClassifyOllamaException
# ============================================================================


class TestClassifyOllamaException:
    def test_401_maps_to_authentication(self) -> None:
        exc = _make_response_error(401)
        out = _classify_ollama_exception(exc)
        assert isinstance(out, AuthenticationError)
        assert out.status_code == 401
        assert out.cause is exc

    def test_403_maps_to_authentication(self) -> None:
        exc = _make_response_error(403)
        out = _classify_ollama_exception(exc)
        assert isinstance(out, AuthenticationError)

    def test_429_maps_to_rate_limit(self) -> None:
        exc = _make_response_error(429)
        out = _classify_ollama_exception(exc)
        assert isinstance(out, RateLimitError)

    def test_400_maps_to_bad_request(self) -> None:
        exc = _make_response_error(400)
        out = _classify_ollama_exception(exc)
        assert isinstance(out, BadRequestError)

    def test_500_maps_to_server(self) -> None:
        exc = _make_response_error(500)
        out = _classify_ollama_exception(exc)
        assert isinstance(out, ServerError)
        assert out.status_code == 500

    def test_response_error_no_status_maps_to_provider(self) -> None:
        exc = ollama.ResponseError("plain")
        # Don't set status_code at all (or set to None)
        exc.status_code = None  # type: ignore[assignment]
        out = _classify_ollama_exception(exc)
        assert isinstance(out, ProviderError)

    def test_request_error_maps_to_network(self) -> None:
        exc = ollama.RequestError("conn refused")
        out = _classify_ollama_exception(exc)
        assert isinstance(out, NetworkError)
        assert out.cause is exc

    def test_httpx_timeout_maps_to_network(self) -> None:
        exc = httpx.TimeoutException("timed out")
        out = _classify_ollama_exception(exc)
        assert isinstance(out, NetworkError)

    def test_httpx_network_error_maps_to_network(self) -> None:
        exc = httpx.NetworkError("net down")
        out = _classify_ollama_exception(exc)
        assert isinstance(out, NetworkError)

    def test_unknown_maps_to_provider(self) -> None:
        exc = RuntimeError("mystery")
        out = _classify_ollama_exception(exc)
        assert isinstance(out, ProviderError)
        assert out.cause is exc


# ============================================================================
# TestStream
# ============================================================================


class TestStream:
    async def test_unknown_model_raises_model_not_found(self) -> None:
        provider = _make_provider(models=["llama3"])
        llm = OllamaLLM(provider)
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
        llm = OllamaLLM(provider)
        client = _patched_client(monkeypatch)
        client.chat.return_value = _aiter(_ok_chunks())

        out = [
            ev
            async for ev in llm.stream(
                model="llama3",
                messages=[Message(role="user", parts=[TextPart(text="hello")])],
                max_output_tokens=64,
            )
        ]
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["StreamStart", "TextDelta", "Usage", "Done"]

    async def test_request_payload_basic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = OllamaLLM(provider)
        client = _patched_client(monkeypatch)
        client.chat.return_value = _aiter(_ok_chunks())

        async for _ in llm.stream(
            model="llama3",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
        ):
            pass
        kwargs = client.chat.call_args.kwargs
        assert kwargs["model"] == "llama3"
        assert kwargs["stream"] is True
        assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
        # No tools / format / options when defaults
        assert "tools" not in kwargs
        assert "format" not in kwargs
        assert "options" not in kwargs

    async def test_request_payload_with_options_and_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = OllamaLLM(provider)
        client = _patched_client(monkeypatch)
        client.chat.return_value = _aiter(_ok_chunks())

        tool = Tool(
            id="search",
            description="Search",
            toolset_id="default",
            args_schema={"type": "object", "properties": {}, "required": []},
        )
        async for _ in llm.stream(
            model="llama3",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            tools=[tool],
            tool_choice="auto",  # should be silently dropped
            temperature=0.5,
            top_p=0.9,
            max_output_tokens=64,
            stop=["END"],
            extended={"keep_alive": "10m", "top_k": 40},
        ):
            pass
        kwargs = client.chat.call_args.kwargs
        assert kwargs["model"] == "llama3"
        assert kwargs["stream"] is True
        # tools translated to nested function shape
        assert kwargs["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            }
        ]
        # options carries sampling + extended OPTIONS keys
        assert kwargs["options"]["temperature"] == 0.5
        assert kwargs["options"]["top_p"] == 0.9
        assert kwargs["options"]["num_predict"] == 64
        assert kwargs["options"]["stop"] == ["END"]
        assert kwargs["options"]["top_k"] == 40
        # top-level extended keys appear at top level
        assert kwargs["keep_alive"] == "10m"
        # Ollama doesn't accept tool_choice — it must NOT be forwarded
        assert "tool_choice" not in kwargs


# ============================================================================
# TestExceptionWrapping
# ============================================================================


class TestExceptionWrapping:
    async def test_pre_stream_response_error_re_raised_as_matrix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = OllamaLLM(provider)
        client = _patched_client(monkeypatch)
        client.chat.side_effect = _make_response_error(401)
        with pytest.raises(AuthenticationError):
            async for _ in llm.stream(
                model="llama3",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_mid_stream_yields_terminal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = OllamaLLM(provider)
        client = _patched_client(monkeypatch)

        async def failing_iter() -> AsyncIterator:
            yield NS(
                model="llama3",
                done=False,
                message=NS(content="hi", thinking=None, tool_calls=None),
            )
            raise _make_response_error(429)

        client.chat.return_value = failing_iter()
        events = [
            ev
            async for ev in llm.stream(
                model="llama3",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            )
        ]
        assert isinstance(events[-1], ChatError)
        assert events[-1].fatal is True
        assert isinstance(events[0], StreamStart)


# ============================================================================
# TestConcurrency
# ============================================================================


class TestConcurrency:
    async def test_semaphore_serialises_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(max_concurrency=1)
        llm = OllamaLLM(provider)
        client = _patched_client(monkeypatch)

        in_flight = 0
        peak = 0

        async def slow_iter() -> AsyncIterator:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            for ch in _ok_chunks():
                yield ch
            in_flight -= 1

        client.chat.side_effect = lambda **_: slow_iter()

        async def consume() -> None:
            async for _ in llm.stream(
                model="llama3",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

        await asyncio.gather(consume(), consume(), consume())
        assert peak == 1


# ============================================================================
# TestPackageReexport (Task 3)
# ============================================================================


class TestPackageReexport:
    def test_reexported(self) -> None:
        import matrix.llm as llm_pkg
        assert "OllamaLLM" in llm_pkg.__all__
        assert llm_pkg.OllamaLLM is OllamaLLM

    def test_others_still_reexported(self) -> None:
        import matrix.llm as llm_pkg
        assert "AnthropicLLM" in llm_pkg.__all__
        assert "GeminiLLM" in llm_pkg.__all__
        assert "OpenResponsesLLM" in llm_pkg.__all__
