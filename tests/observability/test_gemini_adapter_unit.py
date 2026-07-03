"""Unit tests for the Gemini LLM adapter (:mod:`primer.llm.gemini`).

Placed under ``tests/observability`` (an included directory) rather than
``tests/llm`` — which the CI coverage sweep ignores — so the coverage
they exercise is counted. Mocking mirrors the existing ``tests/llm``
adapter tests: pure translators are called directly, stream chunks are
faked with ``SimpleNamespace``, and the google-genai client is mocked at
the ``_get_client`` boundary.
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
import pytest
import respx
from google.genai import types as gtypes
from pydantic import BaseModel, HttpUrl, SecretStr

from primer.llm.gemini import (
    GEMINI_BASE_URL,
    GeminiLLM,
    _StreamState,
    _build_usage,
    _build_sampling_kwargs,
    _discover_gemini_models,
    _extract_extended_kwargs,
    _grounding_to_citations,
    _logprobs_to_event,
    _map_finish_reason,
    _messages_to_gemini,
    _part_to_events,
    _part_to_gemini,
    _response_format_to_gemini,
    _safety_ratings_to_event,
    _translate_chunk,
    _tool_choice_to_gemini,
    _tools_to_gemini,
)
from primer.model.chat import (
    AudioPart,
    Citation,
    DocumentPart,
    Done,
    ExtendedEvent,
    ExtendedPart,
    ImagePart,
    Logprobs,
    MediaDelta,
    Message,
    ReasoningDelta,
    SafetyRatings,
    ServerToolCallStart,
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
from primer.model.except_ import (
    ConfigError,
    ModelNotFoundError,
    PrimerError,
    UnsupportedContentError,
)
from primer.model.provider import (
    GoogleConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)


def _make_provider(
    *,
    api_key: str = "AIza-test",
    models: list[str] | None = None,
    max_concurrency: int = 4,
) -> LLMProvider:
    return LLMProvider(
        id="gemini-default",
        provider=LLMProviderType.GEMINI,
        models=[
            LLMModel(name=name, context_length=1_000_000)
            for name in (models or ["gemini-2.5-flash"])
        ],
        config=GoogleConfig(api_key=SecretStr(api_key)),
        limits=Limits(max_concurrency=max_concurrency),
    )


async def _aiter(items: list) -> AsyncIterator:
    for item in items:
        yield item


# --------------------------------------------------------------------------- #
# Part -> gemini Part mapping                                                  #
# --------------------------------------------------------------------------- #


class TestPartMapping:
    def test_text(self) -> None:
        assert _part_to_gemini(TextPart(text="hi"), {}).text == "hi"

    def test_image_data(self) -> None:
        out = _part_to_gemini(ImagePart(data=b"\x89PNG", mime_type="image/png"), {})
        assert out.inline_data.mime_type == "image/png"
        assert out.inline_data.data == b"\x89PNG"

    def test_image_data_default_mime(self) -> None:
        assert _part_to_gemini(ImagePart(data=b"x"), {}).inline_data.mime_type == "image/png"

    def test_image_url(self) -> None:
        out = _part_to_gemini(ImagePart(url="https://x/i.png", mime_type="image/png"), {})
        assert out.file_data.file_uri == "https://x/i.png"

    def test_image_file_id(self) -> None:
        out = _part_to_gemini(ImagePart(file_id="files/a"), {})
        assert out.file_data.file_uri == "files/a"

    def test_document_data_default_mime(self) -> None:
        out = _part_to_gemini(DocumentPart(data=b"%PDF"), {})
        assert out.inline_data.mime_type == "application/pdf"

    def test_document_url(self) -> None:
        out = _part_to_gemini(DocumentPart(url="https://x/d.pdf", mime_type="application/pdf"), {})
        assert out.file_data.file_uri == "https://x/d.pdf"

    def test_document_file_id(self) -> None:
        out = _part_to_gemini(DocumentPart(file_id="files/doc"), {})
        assert out.file_data.file_uri == "files/doc"

    def test_tool_call(self) -> None:
        out = _part_to_gemini(
            ToolCallPart(id="c1", name="search", arguments={"q": "x"}), {}
        )
        assert out.function_call.name == "search"
        assert out.function_call.args == {"q": "x"}

    def test_tool_result_requires_name_lookup(self) -> None:
        with pytest.raises(UnsupportedContentError, match="no matching ToolCallPart"):
            _part_to_gemini(ToolResultPart(id="c1", output="42"), {})

    def test_tool_result_with_name_lookup(self) -> None:
        out = _part_to_gemini(ToolResultPart(id="c1", output="42"), {"c1": "search"})
        assert out.function_response.name == "search"
        assert out.function_response.response == {"result": "42"}

    def test_audio_data_default_mime(self) -> None:
        out = _part_to_gemini(ExtendedPart(extended=AudioPart(data=b"a")), {})
        assert out.inline_data.mime_type == "audio/mpeg"

    def test_audio_url(self) -> None:
        out = _part_to_gemini(
            ExtendedPart(extended=AudioPart(url="https://x/a.mp3", mime_type="audio/mpeg")), {}
        )
        assert out.file_data.file_uri == "https://x/a.mp3"

    def test_video_data_default_mime(self) -> None:
        out = _part_to_gemini(ExtendedPart(extended=VideoPart(data=b"v")), {})
        assert out.inline_data.mime_type == "video/mp4"

    def test_video_url_with_metadata(self) -> None:
        out = _part_to_gemini(
            ExtendedPart(
                extended=VideoPart(
                    url="https://x/v.mp4",
                    mime_type="video/mp4",
                    start_offset="10s",
                    end_offset="20s",
                    fps=2.0,
                )
            ),
            {},
        )
        assert out.file_data.file_uri == "https://x/v.mp4"
        assert out.video_metadata.fps == 2.0


class TestMessagesToGemini:
    def test_system_concatenated_and_model_role(self) -> None:
        system, contents = _messages_to_gemini(
            [
                Message(role="system", parts=[TextPart(text="a")]),
                Message(role="system", parts=[TextPart(text="b")]),
                Message(role="assistant", parts=[TextPart(text="hey")]),
                Message(role="user", parts=[TextPart(text="hi")]),
            ]
        )
        assert system == "a\n\nb"
        assert [c.role for c in contents] == ["model", "user"]

    def test_no_system(self) -> None:
        system, contents = _messages_to_gemini(
            [Message(role="user", parts=[TextPart(text="hi")])]
        )
        assert system is None
        assert contents[0].role == "user"

    def test_system_non_text_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="system messages"):
            _messages_to_gemini([Message(role="system", parts=[ImagePart(data=b"x")])])

    def test_tool_role_becomes_user_function_response(self) -> None:
        _, contents = _messages_to_gemini(
            [
                Message(
                    role="assistant",
                    parts=[ToolCallPart(id="c1", name="search", arguments={})],
                ),
                Message(role="tool", parts=[ToolResultPart(id="c1", output="42")]),
            ]
        )
        # assistant model content + synthesised user content with function_response
        tool_content = contents[-1]
        assert tool_content.role == "user"
        assert tool_content.parts[0].function_response.name == "search"

    def test_tool_role_non_tool_result_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="tool-role"):
            _messages_to_gemini([Message(role="tool", parts=[TextPart(text="x")])])


class TestToolTranslation:
    def _tools(self) -> list[Tool]:
        return [
            Tool(id="a", description="A", toolset_id="t", args_schema={"type": "object"}),
            Tool(id="b", description="B", toolset_id="t", args_schema={"type": "object"}),
        ]

    def test_empty(self) -> None:
        assert _tools_to_gemini(None) == []
        assert _tools_to_gemini([]) == []

    def test_folds_into_single_tool(self) -> None:
        out = _tools_to_gemini(self._tools())
        assert len(out) == 1
        decls = out[0].function_declarations
        assert [d.name for d in decls] == ["a", "b"]

    def test_tool_choice_none(self) -> None:
        assert _tool_choice_to_gemini(None) is None

    def test_tool_choice_auto(self) -> None:
        cfg = _tool_choice_to_gemini("auto")
        assert cfg.function_calling_config.mode == gtypes.FunctionCallingConfigMode.AUTO

    def test_tool_choice_required(self) -> None:
        cfg = _tool_choice_to_gemini("required")
        assert cfg.function_calling_config.mode == gtypes.FunctionCallingConfigMode.ANY

    def test_tool_choice_none_string(self) -> None:
        cfg = _tool_choice_to_gemini("none")
        assert cfg.function_calling_config.mode == gtypes.FunctionCallingConfigMode.NONE

    def test_tool_choice_specific(self) -> None:
        cfg = _tool_choice_to_gemini("search")
        assert cfg.function_calling_config.mode == gtypes.FunctionCallingConfigMode.ANY
        assert cfg.function_calling_config.allowed_function_names == ["search"]


class _Schema(BaseModel):
    answer: str


class TestResponseFormatAndSampling:
    def test_response_format_none(self) -> None:
        assert _response_format_to_gemini(None) == {}

    def test_response_format_dict(self) -> None:
        out = _response_format_to_gemini({"type": "object"})
        assert out["response_mime_type"] == "application/json"
        assert out["response_schema"] == {"type": "object"}

    def test_response_format_pydantic(self) -> None:
        out = _response_format_to_gemini(_Schema)
        assert out["response_schema"]["properties"]["answer"]["type"] == "string"

    def test_response_format_invalid(self) -> None:
        with pytest.raises(ConfigError, match="Pydantic class or dict"):
            _response_format_to_gemini(123)  # type: ignore[arg-type]

    def test_sampling_kwargs(self) -> None:
        out = _build_sampling_kwargs(
            temperature=0.4, top_p=0.9, max_output_tokens=50, stop=["Z"]
        )
        assert out == {
            "temperature": 0.4,
            "top_p": 0.9,
            "max_output_tokens": 50,
            "stop_sequences": ["Z"],
        }

    def test_sampling_kwargs_empty(self) -> None:
        assert _build_sampling_kwargs(
            temperature=None, top_p=None, max_output_tokens=None, stop=None
        ) == {}

    def test_extended_thinking_and_passthrough(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.gemini")
        out = _extract_extended_kwargs(
            {"thinking_budget": 128, "include_thoughts": True, "seed": 3, "junk": 1}
        )
        assert out["seed"] == 3
        assert isinstance(out["thinking_config"], gtypes.ThinkingConfig)
        assert out["thinking_config"].thinking_budget == 128
        assert any("dropped unknown" in r.message for r in caplog.records)

    def test_extended_empty(self) -> None:
        assert _extract_extended_kwargs(None) == {}
        assert _extract_extended_kwargs({}) == {}


class TestFinishReasonAndUsage:
    def test_stop_without_function_call(self) -> None:
        assert _map_finish_reason("STOP", _StreamState()) == "stop"

    def test_stop_with_function_call(self) -> None:
        assert _map_finish_reason("STOP", _StreamState(saw_function_call=True)) == "tool_use"

    def test_max_tokens(self) -> None:
        assert _map_finish_reason("MAX_TOKENS", _StreamState()) == "max_tokens"

    def test_stop_sequence(self) -> None:
        assert _map_finish_reason("STOP_SEQUENCE", _StreamState()) == "stop_sequence"

    def test_safety_family_is_content_filter(self) -> None:
        for reason in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "BLOCKLIST"):
            assert _map_finish_reason(reason, _StreamState()) == "content_filter"

    def test_malformed_function_call_is_error(self) -> None:
        assert _map_finish_reason("MALFORMED_FUNCTION_CALL", _StreamState()) == "error"

    def test_unknown_is_other(self) -> None:
        assert _map_finish_reason("WHATEVER", _StreamState()) == "other"

    def test_build_usage_none(self) -> None:
        assert _build_usage(None) is None

    def test_build_usage_missing_counts(self) -> None:
        assert _build_usage(NS(prompt_token_count=None, candidates_token_count=5)) is None

    def test_build_usage_full(self) -> None:
        usage = _build_usage(
            NS(
                prompt_token_count=10,
                candidates_token_count=20,
                cached_content_token_count=4,
                thoughts_token_count=7,
            )
        )
        assert usage == Usage(
            input_tokens=10,
            output_tokens=20,
            cached_input_tokens=4,
            reasoning_tokens=7,
            cumulative=True,
        )


class TestPartToEvents:
    def test_text_delta(self) -> None:
        out = _part_to_events(NS(text="hi", thought=None), _StreamState())
        assert out == [TextDelta(text="hi", index=0)]

    def test_reasoning_delta(self) -> None:
        out = _part_to_events(NS(text="ponder", thought=True), _StreamState())
        assert out == [ReasoningDelta(text="ponder", index=0)]

    def test_function_call(self) -> None:
        state = _StreamState()
        out = _part_to_events(
            NS(function_call=NS(id="call_1", name="search", args={"q": "x"})), state
        )
        assert isinstance(out[0], ToolCallStart)
        assert isinstance(out[1], ToolCallDelta)
        assert json.loads(out[1].arguments_delta) == {"q": "x"}
        assert isinstance(out[2], ToolCallEnd)
        assert state.saw_function_call is True

    def test_function_call_synthesises_id(self) -> None:
        out = _part_to_events(
            NS(function_call=NS(id=None, name="fn", args=None)), _StreamState()
        )
        assert out[0].id == "call_0"
        assert out[2].arguments == {}

    def test_inline_audio(self) -> None:
        out = _part_to_events(NS(inline_data=NS(mime_type="audio/mpeg", data=b"a")), _StreamState())
        assert out == [MediaDelta(kind="audio", data=b"a", mime_type="audio/mpeg", index=0)]

    def test_inline_image(self) -> None:
        out = _part_to_events(NS(inline_data=NS(mime_type="image/png", data=b"i")), _StreamState())
        assert out == [MediaDelta(kind="image", data=b"i", mime_type="image/png", index=0)]

    def test_executable_code_and_result(self) -> None:
        state = _StreamState()
        start = _part_to_events(NS(executable_code=NS(code="print(1)")), state)
        assert isinstance(start[0].extended, ServerToolCallStart)
        assert start[0].extended.tool_name == "code_execution"
        result = _part_to_events(
            NS(code_execution_result=NS(outcome="OK", output="1")), state
        )
        assert result[0].extended.result == {"outcome": "OK", "output": "1"}

    def test_unknown_part_ignored(self) -> None:
        assert _part_to_events(NS(), _StreamState()) == []


class TestMetadataEvents:
    def test_grounding_none(self) -> None:
        assert _grounding_to_citations(None, _StreamState()) == []

    def test_grounding_chunks(self) -> None:
        gm = NS(grounding_chunks=[NS(web=NS(uri="https://x", title="T"), retrieved_context=None)])
        out = _grounding_to_citations(gm, _StreamState())
        assert isinstance(out[0].extended, Citation)
        assert out[0].extended.source_url == "https://x"

    def test_safety_empty(self) -> None:
        assert _safety_ratings_to_event(None) == []

    def test_safety_ratings(self) -> None:
        out = _safety_ratings_to_event([NS(category="HARM", probability="LOW")])
        assert isinstance(out[0].extended, SafetyRatings)
        assert out[0].extended.ratings == {"HARM": "LOW"}

    def test_logprobs_none(self) -> None:
        assert _logprobs_to_event(None, _StreamState()) == []

    def test_logprobs_empty_chosen(self) -> None:
        assert _logprobs_to_event(NS(chosen_candidates=[]), _StreamState()) == []

    def test_logprobs_tokens(self) -> None:
        out = _logprobs_to_event(
            NS(chosen_candidates=[NS(token="hi", log_probability=-0.5)]), _StreamState()
        )
        assert isinstance(out[0].extended, Logprobs)
        assert out[0].extended.tokens[0].token == "hi"


class TestTranslateChunk:
    def test_stream_start_emitted_first(self) -> None:
        state = _StreamState()
        chunk = NS(response_id="resp_1", candidates=[], usage_metadata=None)
        out = _translate_chunk(chunk, state, model_name="gemini-x")
        assert isinstance(out[0], StreamStart)
        assert out[0].request_id == "resp_1"
        assert out[0].model == "gemini-x"
        assert state.emitted_stream_start is True

    def test_text_chunk(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        candidate = NS(
            content=NS(parts=[NS(text="hi", thought=None)]),
            grounding_metadata=None,
            safety_ratings=None,
            logprobs_result=None,
            finish_reason=None,
        )
        chunk = NS(response_id="r", candidates=[candidate], usage_metadata=None)
        out = _translate_chunk(chunk, state, model_name="m")
        assert out == [TextDelta(text="hi", index=0)]

    def test_final_chunk_emits_usage_and_done(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        usage_meta = NS(
            prompt_token_count=5,
            candidates_token_count=9,
            cached_content_token_count=None,
            thoughts_token_count=None,
        )
        candidate = NS(
            content=None,
            grounding_metadata=None,
            safety_ratings=None,
            logprobs_result=None,
            finish_reason="STOP",
        )
        chunk = NS(response_id="r", candidates=[candidate], usage_metadata=usage_meta)
        out = _translate_chunk(chunk, state, model_name="m")
        assert isinstance(out[-2], Usage)
        assert isinstance(out[-1], Done)
        assert out[-1].stop_reason == "stop"
        assert out[-1].raw_reason == "STOP"

    def test_final_chunk_enum_finish_reason(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        candidate = NS(
            content=None,
            grounding_metadata=None,
            safety_ratings=None,
            logprobs_result=None,
            finish_reason=NS(name="MAX_TOKENS"),
        )
        chunk = NS(response_id="r", candidates=[candidate], usage_metadata=None)
        out = _translate_chunk(chunk, state, model_name="m")
        assert out[-1].raw_reason == "MAX_TOKENS"
        assert out[-1].stop_reason == "max_tokens"

    def test_no_candidates(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        out = _translate_chunk(NS(response_id="r", candidates=[], usage_metadata=None), state, model_name="m")
        assert out == []


# --------------------------------------------------------------------------- #
# stream() full-drive                                                         #
# --------------------------------------------------------------------------- #


def _text_stream_chunks() -> list:
    first = NS(
        response_id="resp_1",
        candidates=[
            NS(
                content=NS(parts=[NS(text="hi", thought=None)]),
                grounding_metadata=None,
                safety_ratings=None,
                logprobs_result=None,
                finish_reason=None,
            )
        ],
        usage_metadata=None,
    )
    final = NS(
        response_id="resp_1",
        candidates=[
            NS(
                content=None,
                grounding_metadata=None,
                safety_ratings=None,
                logprobs_result=None,
                finish_reason="STOP",
            )
        ],
        usage_metadata=NS(
            prompt_token_count=5,
            candidates_token_count=7,
            cached_content_token_count=None,
            thoughts_token_count=None,
        ),
    )
    return [first, final]


class TestStream:
    async def test_unknown_model_raises(self) -> None:
        llm = GeminiLLM(_make_provider(models=["gemini-2.5-flash"]))
        with pytest.raises(ModelNotFoundError, match="not-real"):
            async for _ in llm.stream(
                model="not-real",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_full_stream_sequence(self) -> None:
        llm = GeminiLLM(_make_provider())
        mock_client = MagicMock()
        mock_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_aiter(_text_stream_chunks())
        )
        with patch.object(llm, "_get_client", return_value=mock_client):
            out = [
                ev
                async for ev in llm.stream(
                    model="gemini-2.5-flash",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                )
            ]
        assert [type(e).__name__ for e in out] == ["StreamStart", "TextDelta", "Usage", "Done"]

    async def test_request_construction(self) -> None:
        llm = GeminiLLM(_make_provider())
        mock_client = MagicMock()
        mock_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_aiter(_text_stream_chunks())
        )
        tool = Tool(id="search", description="S", toolset_id="t", args_schema={"type": "object"})
        with patch.object(llm, "_get_client", return_value=mock_client):
            async for _ in llm.stream(
                model="gemini-2.5-flash",
                messages=[
                    Message(role="system", parts=[TextPart(text="be terse")]),
                    Message(role="user", parts=[TextPart(text="hi")]),
                ],
                temperature=0.2,
                max_output_tokens=64,
                stop=["END"],
                tools=[tool],
                tool_choice="required",
                extended={"seed": 9},
            ):
                pass
        kwargs = mock_client.aio.models.generate_content_stream.call_args.kwargs
        assert kwargs["model"] == "gemini-2.5-flash"
        config = kwargs["config"]
        assert config.system_instruction == "be terse"
        assert config.temperature == 0.2
        assert config.max_output_tokens == 64
        assert config.stop_sequences == ["END"]
        assert config.seed == 9
        assert config.tools[0].function_declarations[0].name == "search"

    async def test_error_before_stream_raises(self) -> None:
        llm = GeminiLLM(_make_provider())
        mock_client = MagicMock()
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        with patch.object(llm, "_get_client", return_value=mock_client):
            with pytest.raises(PrimerError):
                async for _ in llm.stream(
                    model="gemini-2.5-flash",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                ):
                    pass

    async def test_mid_stream_error_yields_chat_error(self) -> None:
        from primer.model.chat import Error as ChatError

        llm = GeminiLLM(_make_provider())

        async def _failing() -> AsyncIterator:
            yield NS(response_id="r", candidates=[], usage_metadata=None)
            raise RuntimeError("mid boom")

        mock_client = MagicMock()
        mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_failing())
        with patch.object(llm, "_get_client", return_value=mock_client):
            out = [
                ev
                async for ev in llm.stream(
                    model="gemini-2.5-flash",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                )
            ]
        assert isinstance(out[-1], ChatError)
        assert out[-1].fatal is True


class _StallStream:
    def __aiter__(self) -> "_StallStream":
        return self

    async def __anext__(self) -> Any:
        await asyncio.sleep(3600)


async def _slow_forever(event: Any) -> AsyncIterator:
    while True:
        await asyncio.sleep(0.005)
        yield event


def _benign_chunk() -> Any:
    return NS(response_id="r", candidates=[], usage_metadata=None)


class TestTimeouts:
    async def test_connect_timeout(self) -> None:
        from primer.model.except_ import ProviderTimeoutError

        llm = GeminiLLM(_make_provider())
        llm._connect_timeout_seconds = 0.05
        mock_client = MagicMock()

        async def _stall(**_: Any) -> Any:
            await asyncio.sleep(3600)

        mock_client.aio.models.generate_content_stream = _stall
        with patch.object(llm, "_get_client", return_value=mock_client):
            with pytest.raises(ProviderTimeoutError):
                async for _ in llm.stream(
                    model="gemini-2.5-flash",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                ):
                    pass

    async def test_stream_stall_timeout(self) -> None:
        from primer.model.except_ import ProviderTimeoutError

        llm = GeminiLLM(_make_provider())
        llm._connect_timeout_seconds = None
        llm._request_timeout_seconds = 0.05
        mock_client = MagicMock()
        mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_StallStream())
        with patch.object(llm, "_get_client", return_value=mock_client):
            with pytest.raises(ProviderTimeoutError) as exc:
                async for _ in llm.stream(
                    model="gemini-2.5-flash",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                ):
                    pass
        assert exc.value.code == "stream_timeout"

    async def test_generation_budget_timeout(self) -> None:
        from primer.model.except_ import ProviderTimeoutError

        llm = GeminiLLM(_make_provider())
        llm._connect_timeout_seconds = None
        llm._request_timeout_seconds = None
        llm._total_timeout_seconds = 0.05
        mock_client = MagicMock()
        mock_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_slow_forever(_benign_chunk())
        )
        with patch.object(llm, "_get_client", return_value=mock_client):
            with pytest.raises(ProviderTimeoutError) as exc:
                async for _ in llm.stream(
                    model="gemini-2.5-flash",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                ):
                    pass
        assert exc.value.code == "generation_timeout"

    async def test_trace_llm_io(self) -> None:
        llm = GeminiLLM(_make_provider(), trace_llm_io=True)
        mock_client = MagicMock()
        mock_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_aiter(_text_stream_chunks())
        )
        with patch.object(llm, "_get_client", return_value=mock_client):
            out = [
                ev
                async for ev in llm.stream(
                    model="gemini-2.5-flash",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                )
            ]
        assert any(type(e).__name__ == "Done" for e in out)


class TestAdapterLifecycle:
    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", LLMProviderType.ANTHROPIC)
        with pytest.raises(ConfigError, match="GEMINI"):
            GeminiLLM(provider)

    def test_rejects_wrong_config_type(self) -> None:
        from primer.model.provider import OpenResponsesConfig

        provider = LLMProvider(
            id="g",
            provider=LLMProviderType.GEMINI,
            models=[LLMModel(name="gemini-2.5-flash", context_length=1024)],
            config=OpenResponsesConfig(  # type: ignore[arg-type]
                url=HttpUrl("https://api.openai.com/v1/"),
                api_key=SecretStr("sk-x"),
            ),
            limits=Limits(max_concurrency=1),
        )
        with pytest.raises(ConfigError, match="GoogleConfig"):
            GeminiLLM(provider)

    def test_accepts_empty_api_key(self) -> None:
        assert GeminiLLM(_make_provider(api_key=""))._client is None

    async def test_list_models(self) -> None:
        llm = GeminiLLM(_make_provider(models=["x", "y"]))
        assert list(await llm.list_models()) == ["x", "y"]

    def test_get_client_lazy_and_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inst = MagicMock()
        monkeypatch.setattr("primer.llm.gemini.genai.Client", MagicMock(return_value=inst))
        llm = GeminiLLM(_make_provider())
        assert llm._client is None
        assert llm._get_client() is inst
        assert llm._get_client() is inst  # cached

    async def test_aclose_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inst = MagicMock()
        inst.close = MagicMock()
        monkeypatch.setattr("primer.llm.gemini.genai.Client", MagicMock(return_value=inst))
        llm = GeminiLLM(_make_provider())
        llm._get_client()
        await llm.aclose()
        await llm.aclose()
        inst.close.assert_called_once()

    async def test_count_tokens_delegates(self) -> None:
        llm = GeminiLLM(_make_provider())
        mock_client = MagicMock()
        with patch.object(llm, "_get_client", return_value=mock_client):
            with patch(
                "primer.llm.gemini.count_tokens_gemini",
                new=AsyncMock(return_value=55),
            ) as mock_count:
                n = await llm.count_tokens(
                    model="gemini-2.5-flash",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                )
        assert n == 55
        mock_count.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Model discovery (respx-mocked HTTP)                                          #
# --------------------------------------------------------------------------- #


class TestDiscover:
    @respx.mock
    async def test_filters_and_maps(self) -> None:
        respx.get(f"{GEMINI_BASE_URL}/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "displayName": "Flash",
                            "inputTokenLimit": 1048576,
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/text-embedding-004",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                        "not-a-dict-entry",
                        {
                            # No name -> skipped.
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            # Bare "models/" prefix -> empty id -> skipped.
                            "name": "models/",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/gemini-2.5-pro",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ]
                },
            )
        )
        out = await _discover_gemini_models(GoogleConfig(api_key=SecretStr("k")))
        assert [m["name"] for m in out] == ["gemini-2.5-flash", "gemini-2.5-pro"]
        assert out[0]["context_length"] == 1048576
        assert "context_length" not in out[1]

    @respx.mock
    async def test_paginates(self) -> None:
        route = respx.get(f"{GEMINI_BASE_URL}/models").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "models": [
                            {"name": "models/a", "supportedGenerationMethods": ["generateContent"]}
                        ],
                        "nextPageToken": "P2",
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "models": [
                            {"name": "models/b", "supportedGenerationMethods": ["generateContent"]}
                        ]
                    },
                ),
            ]
        )
        out = await _discover_gemini_models(GoogleConfig(api_key=SecretStr("k")))
        assert [m["name"] for m in out] == ["a", "b"]
        assert len(route.calls) == 2

    @respx.mock
    async def test_401_raises(self) -> None:
        respx.get(f"{GEMINI_BASE_URL}/models").mock(
            return_value=httpx.Response(401, json={"error": "bad"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _discover_gemini_models(GoogleConfig(api_key=SecretStr("bad")))
