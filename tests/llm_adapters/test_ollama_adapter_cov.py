"""Coverage tests for the OllamaLLM adapter.

Placed outside ``tests/llm/`` so ``primer.llm.ollama`` counts in the CI
unit sweep. Pure helpers are tested directly; the streaming surface is
driven through a patched ``ollama.AsyncClient`` returning fake chunks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import ollama
import pytest
from pydantic import BaseModel as PydanticBaseModel
from pydantic import HttpUrl, SecretStr

from primer.llm.ollama import (
    OllamaLLM,
    _OPTIONS_KEYS,
    _StreamState,
    _TOP_LEVEL_KEYS,
    _build_options_and_kwargs,
    _classify_ollama_exception,
    _map_stop_reason,
    _maybe_log_unsupported_tool_choice,
    _messages_to_ollama,
    _next_index,
    _response_format_to_ollama,
    _tools_to_ollama,
    _translate_chunk,
)
from primer.model.chat import (
    AudioPart,
    DocumentPart,
    Done,
    Error as ChatError,
    ExtendedPart,
    ImagePart,
    Message,
    ReasoningDelta,
    StreamStart,
    TextDelta,
    TextPart,
    Tool,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    Usage,
    VideoPart,
)
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    ConfigError,
    ModelNotFoundError,
    NetworkError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
    UnsupportedContentError,
)
from primer.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OllamaConfig,
)


def _make_provider(
    *,
    url: str = "http://localhost:11434",
    api_key: str | None = None,
    models: list[str] | None = None,
    max_concurrency: int = 4,
    total_timeout_seconds: float | None = None,
) -> LLMProvider:
    return LLMProvider(
        id="ollama-cov",
        provider=LLMProviderType.OLLAMA,
        models=[
            LLMModel(name=name, context_length=8192) for name in (models or ["llama3"])
        ],
        config=OllamaConfig(
            url=HttpUrl(url),
            api_key=SecretStr(api_key) if api_key is not None else None,
        ),
        limits=Limits(
            max_concurrency=max_concurrency,
            total_timeout_seconds=total_timeout_seconds,
        ),
    )


async def _aiter(items: list) -> AsyncIterator:
    for item in items:
        yield item


def _patched_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_instance = MagicMock()
    mock_instance.chat = AsyncMock()
    cls_mock = MagicMock(return_value=mock_instance)
    monkeypatch.setattr("primer.llm.ollama.ollama.AsyncClient", cls_mock)
    return mock_instance


def _ok_chunks() -> list[Any]:
    return [
        NS(model="llama3", done=False, message=NS(content="hi", thinking=None, tool_calls=None)),
        NS(
            model="llama3",
            done=True,
            done_reason="stop",
            prompt_eval_count=5,
            eval_count=3,
            message=NS(content="", thinking=None, tool_calls=None),
        ),
    ]


def _response_error(status_code: int | None, msg: str = "boom") -> ollama.ResponseError:
    exc = ollama.ResponseError(msg)
    exc.status_code = status_code  # type: ignore[assignment]
    return exc


# --------------------------------------------------------------------------- #
# _classify_ollama_exception                                                  #
# --------------------------------------------------------------------------- #


class TestClassify:
    @pytest.mark.parametrize("status", [401, 403])
    def test_auth(self, status: int) -> None:
        exc = _response_error(status)
        out = _classify_ollama_exception(exc)
        assert isinstance(out, AuthenticationError)
        assert out.status_code == status
        assert out.cause is exc

    def test_rate_limit(self) -> None:
        assert isinstance(_classify_ollama_exception(_response_error(429)), RateLimitError)

    def test_bad_request(self) -> None:
        assert isinstance(_classify_ollama_exception(_response_error(400)), BadRequestError)

    def test_server_error(self) -> None:
        out = _classify_ollama_exception(_response_error(503))
        assert isinstance(out, ServerError)
        assert out.status_code == 503

    def test_no_status_provider_error(self) -> None:
        assert isinstance(_classify_ollama_exception(_response_error(None)), ProviderError)

    def test_request_error_network(self) -> None:
        out = _classify_ollama_exception(ollama.RequestError("conn refused"))
        assert isinstance(out, NetworkError)

    def test_httpx_timeout_network(self) -> None:
        assert isinstance(
            _classify_ollama_exception(httpx.TimeoutException("t")), NetworkError
        )

    def test_httpx_network_error_network(self) -> None:
        assert isinstance(_classify_ollama_exception(httpx.NetworkError("down")), NetworkError)

    def test_unknown_provider_error(self) -> None:
        exc = RuntimeError("mystery")
        out = _classify_ollama_exception(exc)
        assert isinstance(out, ProviderError)
        assert out.cause is exc


# --------------------------------------------------------------------------- #
# _messages_to_ollama                                                         #
# --------------------------------------------------------------------------- #


class TestMessagesToOllama:
    def test_simple_text(self) -> None:
        assert _messages_to_ollama(
            [Message(role="user", parts=[TextPart(text="hi")])]
        ) == [{"role": "user", "content": "hi"}]

    def test_multiple_text_joined_newline(self) -> None:
        assert _messages_to_ollama(
            [Message(role="user", parts=[TextPart(text="a"), TextPart(text="b")])]
        ) == [{"role": "user", "content": "a\nb"}]

    def test_image_data_appended(self) -> None:
        out = _messages_to_ollama(
            [
                Message(
                    role="user",
                    parts=[TextPart(text="d"), ImagePart(data=b"\x89PNG", mime_type="image/png")],
                )
            ]
        )
        assert out == [{"role": "user", "content": "d", "images": [b"\x89PNG"]}]

    def test_image_url_without_data_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="inline image data"):
            _messages_to_ollama(
                [Message(role="user", parts=[ImagePart(url="https://e/i.png")])]
            )

    def test_document_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="documents"):
            _messages_to_ollama(
                [Message(role="user", parts=[DocumentPart(data=b"%PDF", mime_type="application/pdf")])]
            )

    def test_audio_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="audio"):
            _messages_to_ollama(
                [Message(role="user", parts=[ExtendedPart(extended=AudioPart(data=b"x", mime_type="audio/mp3"))])]
            )

    def test_video_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="video"):
            _messages_to_ollama(
                [Message(role="user", parts=[ExtendedPart(extended=VideoPart(url="https://e/v.mp4"))])]
            )

    def test_assistant_tool_call_nested(self) -> None:
        out = _messages_to_ollama(
            [
                Message(
                    role="assistant",
                    parts=[
                        TextPart(text="check"),
                        ToolCallPart(id="c1", name="search", arguments={"q": "w"}),
                    ],
                )
            ]
        )
        assert out == [
            {
                "role": "assistant",
                "content": "check",
                "tool_calls": [{"function": {"name": "search", "arguments": {"q": "w"}}}],
            }
        ]

    def test_tool_role_id_to_name_lookup(self) -> None:
        out = _messages_to_ollama(
            [
                Message(role="assistant", parts=[ToolCallPart(id="c1", name="search", arguments={})]),
                Message(role="tool", parts=[ToolResultPart(id="c1", output="42")]),
            ]
        )
        assert out[-1] == {"role": "tool", "content": "42", "tool_name": "search"}

    def test_tool_role_unknown_id_empty_name(self) -> None:
        out = _messages_to_ollama(
            [Message(role="tool", parts=[ToolResultPart(id="unknown", output="42")])]
        )
        assert out == [{"role": "tool", "content": "42", "tool_name": ""}]

    def test_tool_role_non_result_raises(self) -> None:
        msg = Message.model_construct(role="tool", parts=[TextPart(text="oops")])
        with pytest.raises(UnsupportedContentError, match="tool-role messages"):
            _messages_to_ollama([msg])


# --------------------------------------------------------------------------- #
# tools / tool_choice / response_format / options                            #
# --------------------------------------------------------------------------- #


class TestTools:
    def test_none(self) -> None:
        assert _tools_to_ollama(None) is None

    def test_empty(self) -> None:
        assert _tools_to_ollama([]) is None

    def test_single_nested(self) -> None:
        tool = Tool(
            id="w",
            description="weather",
            toolset_id="kit",
            args_schema={"type": "object", "properties": {"c": {"type": "string"}}},
        )
        out = _tools_to_ollama([tool])
        assert out[0]["function"]["name"] == "w"
        assert out[0]["type"] == "function"


class TestToolChoice:
    def test_none_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.ollama")
        _maybe_log_unsupported_tool_choice(None)
        assert all("tool_choice" not in r.message for r in caplog.records)

    def test_non_none_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.ollama")
        _maybe_log_unsupported_tool_choice("auto")
        assert any("tool_choice" in r.message for r in caplog.records)


class TestResponseFormat:
    def test_none(self) -> None:
        assert _response_format_to_ollama(None) is None

    def test_dict(self) -> None:
        schema = {"type": "object"}
        assert _response_format_to_ollama(schema) == schema

    def test_pydantic(self) -> None:
        class A(PydanticBaseModel):
            value: int

        out = _response_format_to_ollama(A)
        assert "value" in out["properties"]

    def test_invalid_raises(self) -> None:
        with pytest.raises(ConfigError, match="response_format"):
            _response_format_to_ollama(42)  # type: ignore[arg-type]


class TestOptions:
    def test_all_none(self) -> None:
        options, top = _build_options_and_kwargs(
            temperature=None, top_p=None, max_output_tokens=None, stop=None, extended=None
        )
        assert options == {} and top == {}

    def test_sampling_to_options(self) -> None:
        options, top = _build_options_and_kwargs(
            temperature=0.7, top_p=0.9, max_output_tokens=128, stop=["END"], extended=None
        )
        assert options == {"temperature": 0.7, "top_p": 0.9, "num_predict": 128, "stop": ["END"]}
        assert top == {}

    @pytest.mark.parametrize("key", sorted(_OPTIONS_KEYS))
    def test_options_keys_route(self, key: str) -> None:
        options, top = _build_options_and_kwargs(
            temperature=None, top_p=None, max_output_tokens=None, stop=None, extended={key: 1}
        )
        assert options == {key: 1} and top == {}

    @pytest.mark.parametrize("key", sorted(_TOP_LEVEL_KEYS))
    def test_top_level_keys_route(self, key: str) -> None:
        options, top = _build_options_and_kwargs(
            temperature=None, top_p=None, max_output_tokens=None, stop=None, extended={key: "v"}
        )
        assert options == {} and top == {key: "v"}

    def test_unknown_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.ollama")
        options, top = _build_options_and_kwargs(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=None,
            extended={"frobnicate": True, "wibble": 42},
        )
        assert options == {} and top == {}
        assert any("frobnicate" in r.message and "wibble" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# stop-reason / index / translate                                            #
# --------------------------------------------------------------------------- #


class TestStopReason:
    def test_stop_no_tool(self) -> None:
        assert _map_stop_reason("stop", _StreamState()) == "stop"

    def test_stop_with_tool(self) -> None:
        state = _StreamState()
        state.saw_tool_call = True
        assert _map_stop_reason("stop", state) == "tool_use"

    @pytest.mark.parametrize("raw, mapped", [("length", "max_tokens"), ("load", "other"), ("x", "other"), (None, "other")])
    def test_others(self, raw: str | None, mapped: str) -> None:
        assert _map_stop_reason(raw, _StreamState()) == mapped


class TestNextIndex:
    def test_increments(self) -> None:
        state = _StreamState()
        assert (_next_index(state), _next_index(state), _next_index(state)) == (0, 1, 2)
        assert state.next_index == 3


class TestTranslate:
    def test_first_chunk_stream_start(self) -> None:
        state = _StreamState()
        chunk = NS(model="llama3", done=False, message=NS(content="", thinking=None, tool_calls=None))
        out = _translate_chunk(chunk, state, model_name="llama3")
        assert any(isinstance(e, StreamStart) for e in out)
        assert state.emitted_stream_start is True

    def test_stream_start_falls_back_to_caller_model(self) -> None:
        state = _StreamState()
        chunk = NS(model=None, done=False, message=NS(content="", thinking=None, tool_calls=None))
        out = _translate_chunk(chunk, state, model_name="caller")
        start = next(e for e in out if isinstance(e, StreamStart))
        assert start.model == "caller"

    def test_text_delta(self) -> None:
        state = _StreamState()
        chunk = NS(model="m", done=False, message=NS(content="hi", thinking=None, tool_calls=None))
        out = _translate_chunk(chunk, state, model_name="m")
        deltas = [e for e in out if isinstance(e, TextDelta)]
        assert deltas[0].text == "hi"

    def test_reasoning_delta(self) -> None:
        state = _StreamState()
        chunk = NS(model="m", done=False, message=NS(content=None, thinking="hmm", tool_calls=None))
        out = _translate_chunk(chunk, state, model_name="m")
        assert any(isinstance(e, ReasoningDelta) and e.text == "hmm" for e in out)

    def test_tool_calls_atomic(self) -> None:
        state = _StreamState()
        chunk = NS(
            model="m",
            done=False,
            message=NS(
                content=None,
                thinking=None,
                tool_calls=[NS(function=NS(name="search", arguments={"q": "w"}))],
            ),
        )
        out = _translate_chunk(chunk, state, model_name="m")
        assert [type(e).__name__ for e in out] == [
            "StreamStart",
            "ToolCallStart",
            "ToolCallDelta",
            "ToolCallEnd",
        ]
        start = next(e for e in out if isinstance(e, ToolCallStart))
        end = next(e for e in out if isinstance(e, ToolCallEnd))
        assert start.id == "call_0" and start.name == "search"
        assert end.arguments == {"q": "w"}
        assert state.saw_tool_call is True

    def test_tool_call_missing_function_defaults(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        chunk = NS(
            model="m",
            done=False,
            message=NS(content=None, thinking=None, tool_calls=[NS(function=None)]),
        )
        out = _translate_chunk(chunk, state, model_name="m")
        end = next(e for e in out if isinstance(e, ToolCallEnd))
        assert end.arguments == {}
        start = next(e for e in out if isinstance(e, ToolCallStart))
        assert start.name == ""

    def test_done_usage_then_done(self) -> None:
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
        assert [type(e).__name__ for e in out] == ["Usage", "Done"]
        assert out[0].input_tokens == 5 and out[0].output_tokens == 3
        assert out[1].raw_reason == "stop"

    def test_done_without_tokens_omits_usage(self) -> None:
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
        assert [type(e).__name__ for e in out] == ["Done"]

    def test_done_no_reason_uses_unknown(self) -> None:
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
        assert isinstance(out[0], Done) and out[0].raw_reason == "unknown"

    def test_none_message_chunk(self) -> None:
        state = _StreamState()
        chunk = NS(model="m", done=False, message=None)
        out = _translate_chunk(chunk, state, model_name="m")
        assert [type(e).__name__ for e in out] == ["StreamStart"]


# --------------------------------------------------------------------------- #
# Adapter surface                                                             #
# --------------------------------------------------------------------------- #


class TestConstructor:
    def test_valid(self) -> None:
        assert OllamaLLM(_make_provider())._client is None

    def test_with_api_key(self) -> None:
        llm = OllamaLLM(_make_provider(api_key="tok"))
        assert llm._config.api_key.get_secret_value() == "tok"

    def test_wrong_provider_type_raises(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", LLMProviderType.OPENCHAT)
        with pytest.raises(ConfigError, match="OLLAMA"):
            OllamaLLM(provider)

    def test_wrong_config_type_raises(self) -> None:
        from primer.model.provider import OpenResponsesConfig

        provider = LLMProvider(
            id="x",
            provider=LLMProviderType.OLLAMA,
            models=[LLMModel(name="llama3", context_length=8192)],
            config=OpenResponsesConfig(url=HttpUrl("https://x/v1/"), api_key=SecretStr("sk-x")),
            limits=Limits(max_concurrency=1),
        )
        with pytest.raises(ConfigError, match="OllamaConfig"):
            OllamaLLM(provider)

    def test_logs_init(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="primer.llm.ollama")
        OllamaLLM(_make_provider(models=["llama3", "mistral"]))
        assert any("Ollama adapter initialized" in r.message for r in caplog.records)


class TestGetClient:
    def test_no_key_no_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cls_mock = MagicMock(return_value=MagicMock())
        monkeypatch.setattr("primer.llm.ollama.ollama.AsyncClient", cls_mock)
        OllamaLLM(_make_provider())._get_client()
        kwargs = cls_mock.call_args.kwargs
        assert kwargs["headers"] is None
        assert "localhost:11434" in kwargs["host"]

    def test_api_key_sets_bearer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cls_mock = MagicMock(return_value=MagicMock())
        monkeypatch.setattr("primer.llm.ollama.ollama.AsyncClient", cls_mock)
        OllamaLLM(_make_provider(api_key="tok"))._get_client()
        assert cls_mock.call_args.kwargs["headers"] == {"Authorization": "Bearer tok"}

    def test_empty_key_no_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cls_mock = MagicMock(return_value=MagicMock())
        monkeypatch.setattr("primer.llm.ollama.ollama.AsyncClient", cls_mock)
        OllamaLLM(_make_provider(api_key=""))._get_client()
        assert cls_mock.call_args.kwargs["headers"] is None

    def test_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cls_mock = MagicMock(return_value=MagicMock())
        monkeypatch.setattr("primer.llm.ollama.ollama.AsyncClient", cls_mock)
        llm = OllamaLLM(_make_provider())
        assert llm._get_client() is llm._get_client()
        assert cls_mock.call_count == 1


class TestListModelsAndTokens:
    async def test_list_models(self) -> None:
        llm = OllamaLLM(_make_provider(models=["llama3", "mistral"]))
        assert list(await llm.list_models()) == ["llama3", "mistral"]

    async def test_count_tokens_delegates(self) -> None:
        llm = OllamaLLM(_make_provider())
        with patch("primer.llm.ollama.count_tokens_hf", return_value=17) as mock:
            n = await llm.count_tokens(
                model="llama3", messages=[Message(role="user", parts=[TextPart(text="hi")])], tools=None
            )
        assert n == 17
        mock.assert_called_once()


class TestAclose:
    async def test_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _patched_client(monkeypatch)
        client.close = AsyncMock()
        llm = OllamaLLM(_make_provider())
        llm._get_client()
        await llm.aclose()
        await llm.aclose()
        assert client.close.await_count == 1


class TestStream:
    async def test_unknown_model_raises(self) -> None:
        llm = OllamaLLM(_make_provider(models=["llama3"]))
        with pytest.raises(ModelNotFoundError, match="not-real"):
            async for _ in llm.stream(
                model="not-real", messages=[Message(role="user", parts=[TextPart(text="hi")])]
            ):
                pass

    async def test_happy_path_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OllamaLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.return_value = _aiter(_ok_chunks())
        kinds = [
            type(e).__name__
            async for e in llm.stream(
                model="llama3",
                messages=[Message(role="user", parts=[TextPart(text="hello")])],
                max_output_tokens=64,
            )
        ]
        assert kinds == ["StreamStart", "TextDelta", "Usage", "Done"]

    async def test_request_payload_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OllamaLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.return_value = _aiter(_ok_chunks())
        async for _ in llm.stream(
            model="llama3", messages=[Message(role="user", parts=[TextPart(text="hi")])]
        ):
            pass
        kwargs = client.chat.call_args.kwargs
        assert kwargs["model"] == "llama3" and kwargs["stream"] is True
        assert "tools" not in kwargs and "format" not in kwargs and "options" not in kwargs

    async def test_request_payload_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OllamaLLM(_make_provider())
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
            tool_choice="auto",
            temperature=0.5,
            max_output_tokens=64,
            stop=["END"],
            response_format={"type": "object"},
            extended={"keep_alive": "10m", "top_k": 40},
        ):
            pass
        kwargs = client.chat.call_args.kwargs
        assert kwargs["tools"][0]["function"]["name"] == "search"
        assert kwargs["options"]["num_predict"] == 64
        assert kwargs["options"]["stop"] == ["END"]
        assert kwargs["options"]["top_k"] == 40
        assert kwargs["format"] == {"type": "object"}
        assert kwargs["keep_alive"] == "10m"
        assert "tool_choice" not in kwargs

    async def test_trace_llm_io_records_messages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OllamaLLM(_make_provider(), trace_llm_io=True)
        client = _patched_client(monkeypatch)
        client.chat.return_value = _aiter(_ok_chunks())
        kinds = [
            type(e).__name__
            async for e in llm.stream(
                model="llama3",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
                max_output_tokens=32,
            )
        ]
        assert kinds[-1] == "Done"

    async def test_pre_stream_error_reraised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OllamaLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.side_effect = _response_error(401)
        with pytest.raises(AuthenticationError):
            async for _ in llm.stream(
                model="llama3", messages=[Message(role="user", parts=[TextPart(text="hi")])]
            ):
                pass

    async def test_mid_stream_error_yields_chat_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OllamaLLM(_make_provider())
        client = _patched_client(monkeypatch)

        async def failing() -> AsyncIterator:
            yield NS(model="llama3", done=False, message=NS(content="hi", thinking=None, tool_calls=None))
            raise _response_error(429)

        client.chat.return_value = failing()
        events = [
            e
            async for e in llm.stream(
                model="llama3", messages=[Message(role="user", parts=[TextPart(text="hi")])]
            )
        ]
        assert isinstance(events[0], StreamStart)
        assert isinstance(events[-1], ChatError) and events[-1].fatal is True

    async def test_connect_timeout_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OllamaLLM(_make_provider())
        _patched_client(monkeypatch)

        async def raise_connect_timeout(*_a, **_k):
            raise ProviderTimeoutError("connect stalled", code="connect_timeout")

        monkeypatch.setattr(
            "primer.llm.ollama._open_with_connect_timeout", raise_connect_timeout
        )
        with pytest.raises(ProviderTimeoutError) as info:
            async for _ in llm.stream(
                model="llama3", messages=[Message(role="user", parts=[TextPart(text="hi")])]
            ):
                pass
        assert info.value.code == "connect_timeout"

    async def test_generation_budget_maps_to_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from primer.llm._timeout import GenerationBudgetExceeded

        llm = OllamaLLM(_make_provider(total_timeout_seconds=30.0))
        client = _patched_client(monkeypatch)
        client.chat.return_value = _aiter(_ok_chunks())

        async def budget_iter(*_a, **_k):
            raise GenerationBudgetExceeded("over")
            yield  # pragma: no cover

        monkeypatch.setattr("primer.llm.ollama._iter_with_timeout", budget_iter)
        with pytest.raises(ProviderTimeoutError) as info:
            async for _ in llm.stream(
                model="llama3", messages=[Message(role="user", parts=[TextPart(text="hi")])]
            ):
                pass
        assert info.value.code == "generation_timeout"

    async def test_stall_timeout_maps_to_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OllamaLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.return_value = _aiter(_ok_chunks())

        async def stall_iter(*_a, **_k):
            raise TimeoutError("stall")
            yield  # pragma: no cover

        monkeypatch.setattr("primer.llm.ollama._iter_with_timeout", stall_iter)
        with pytest.raises(ProviderTimeoutError) as info:
            async for _ in llm.stream(
                model="llama3", messages=[Message(role="user", parts=[TextPart(text="hi")])]
            ):
                pass
        assert info.value.code == "stream_timeout"


class TestConcurrency:
    async def test_semaphore_serialises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OllamaLLM(_make_provider(max_concurrency=1))
        client = _patched_client(monkeypatch)
        in_flight = 0
        peak = 0

        async def slow() -> AsyncIterator:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            for ch in _ok_chunks():
                yield ch
            in_flight -= 1

        client.chat.side_effect = lambda **_: slow()

        async def consume() -> None:
            async for _ in llm.stream(
                model="llama3", messages=[Message(role="user", parts=[TextPart(text="hi")])]
            ):
                pass

        await asyncio.gather(consume(), consume(), consume())
        assert peak == 1


class TestPackageReexport:
    def test_reexported(self) -> None:
        import primer.llm as pkg

        assert "OllamaLLM" in pkg.__all__
        assert pkg.OllamaLLM is OllamaLLM
