"""Unit tests for the Gemini LLM adapter."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from matrix.llm.gemini import GeminiLLM
from matrix.model.except_ import ConfigError
from matrix.model.provider import (
    GoogleConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)


# ------------------------------------------------------------------------- #
# Test fixtures                                                              #
# ------------------------------------------------------------------------- #


def _make_provider(
    *,
    api_key: str = "api-key-test",
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


# ------------------------------------------------------------------------- #
# TestConstructor                                                            #
# ------------------------------------------------------------------------- #


class TestConstructor:
    def test_accepts_valid_config(self) -> None:
        provider = _make_provider()
        llm = GeminiLLM(provider)
        assert llm._client is None  # lazy

    def test_rejects_empty_api_key(self) -> None:
        provider = _make_provider(api_key="")
        with pytest.raises(ConfigError, match="api_key is required"):
            GeminiLLM(provider)

    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        # Tamper with the validated provider for the test.
        object.__setattr__(provider, "provider", "openresponses")  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="GEMINI"):
            GeminiLLM(provider)

    def test_rejects_wrong_config_type(self) -> None:
        from pydantic import HttpUrl
        from matrix.model.provider import OpenResponsesConfig

        # Build a provider with the right enum but wrong config class.
        provider = LLMProvider(
            id="g",
            provider=LLMProviderType.GEMINI,
            models=[LLMModel(name="gemini-2.5-flash", context_length=1024)],
            config=OpenResponsesConfig(  # type: ignore[arg-type]
                url=HttpUrl("https://example.com/v1/"),
                api_key=SecretStr("sk-x"),
            ),
            limits=Limits(max_concurrency=1),
        )
        with pytest.raises(ConfigError, match="GoogleConfig"):
            GeminiLLM(provider)

    def test_initialises_semaphore_to_max_concurrency(self) -> None:
        provider = _make_provider(max_concurrency=3)
        llm = GeminiLLM(provider)
        assert isinstance(llm._semaphore, asyncio.Semaphore)
        assert llm._semaphore._value == 3  # type: ignore[attr-defined]

    def test_logs_init_with_structured_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="matrix.llm.gemini")
        provider = _make_provider(
            models=["gemini-2.5-flash", "gemini-2.5-pro"],
            max_concurrency=2,
        )
        GeminiLLM(provider)
        records = [
            r for r in caplog.records if "Gemini adapter initialized" in r.message
        ]
        assert len(records) == 1
        record = records[0]
        assert record.provider_id == "gemini-default"  # type: ignore[attr-defined]
        assert record.models == [  # type: ignore[attr-defined]
            "gemini-2.5-flash",
            "gemini-2.5-pro",
        ]
        assert record.max_concurrency == 2  # type: ignore[attr-defined]


# ------------------------------------------------------------------------- #
# TestListModels                                                             #
# ------------------------------------------------------------------------- #


class TestListModels:
    async def test_returns_configured_model_names(self) -> None:
        provider = _make_provider(models=["gemini-2.5-flash", "gemini-2.5-pro"])
        llm = GeminiLLM(provider)
        models = list(await llm.list_models())
        assert models == ["gemini-2.5-flash", "gemini-2.5-pro"]

    async def test_does_not_call_upstream(self) -> None:
        provider = _make_provider()
        llm = GeminiLLM(provider)
        with patch.object(GeminiLLM, "_get_client") as mock_get_client:
            await llm.list_models()
            mock_get_client.assert_not_called()


# ------------------------------------------------------------------------- #
# Input mapping tests                                                        #
# ------------------------------------------------------------------------- #


from matrix.llm.gemini import _messages_to_gemini, _part_to_gemini
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


class TestInputMapping:
    def test_text_part(self) -> None:
        out = _part_to_gemini(TextPart(text="hello"), {})
        assert out.text == "hello"

    def test_image_part_with_data_uses_inline_data(self) -> None:
        out = _part_to_gemini(
            ImagePart(data=b"\x89PNG", mime_type="image/png"), {}
        )
        assert out.inline_data is not None
        assert out.inline_data.mime_type == "image/png"
        assert out.inline_data.data == b"\x89PNG"

    def test_image_part_without_mime_defaults_to_png(self) -> None:
        out = _part_to_gemini(ImagePart(data=b"raw"), {})
        assert out.inline_data.mime_type == "image/png"

    def test_image_part_with_url_uses_file_data(self) -> None:
        out = _part_to_gemini(
            ImagePart(url="https://example.com/img.png", mime_type="image/png"), {}
        )
        assert out.file_data is not None
        assert out.file_data.file_uri == "https://example.com/img.png"
        assert out.file_data.mime_type == "image/png"

    def test_image_part_with_file_id_uses_file_data(self) -> None:
        out = _part_to_gemini(
            ImagePart(file_id="files/abc-123", mime_type="image/png"), {}
        )
        assert out.file_data.file_uri == "files/abc-123"

    def test_document_part_with_data(self) -> None:
        out = _part_to_gemini(
            DocumentPart(data=b"%PDF-1.4", mime_type="application/pdf"), {}
        )
        assert out.inline_data.mime_type == "application/pdf"
        assert out.inline_data.data == b"%PDF-1.4"

    def test_document_part_default_mime(self) -> None:
        out = _part_to_gemini(DocumentPart(data=b"%PDF"), {})
        assert out.inline_data.mime_type == "application/pdf"

    def test_audio_part_with_data(self) -> None:
        part = ExtendedPart(extended=AudioPart(data=b"raw", mime_type="audio/wav"))
        out = _part_to_gemini(part, {})
        assert out.inline_data.mime_type == "audio/wav"

    def test_audio_part_with_url(self) -> None:
        part = ExtendedPart(
            extended=AudioPart(url="https://example.com/a.mp3", mime_type="audio/mpeg")
        )
        out = _part_to_gemini(part, {})
        assert out.file_data.file_uri == "https://example.com/a.mp3"

    def test_video_part_with_url_and_metadata(self) -> None:
        part = ExtendedPart(
            extended=VideoPart(
                url="https://example.com/v.mp4",
                mime_type="video/mp4",
                start_offset="10s",
                end_offset="60s",
                fps=2.0,
            )
        )
        out = _part_to_gemini(part, {})
        assert out.file_data.file_uri == "https://example.com/v.mp4"
        assert out.video_metadata is not None
        assert out.video_metadata.start_offset == "10s"
        assert out.video_metadata.end_offset == "60s"
        assert out.video_metadata.fps == 2.0

    def test_video_part_without_metadata_fields(self) -> None:
        part = ExtendedPart(
            extended=VideoPart(url="https://example.com/v.mp4", mime_type="video/mp4")
        )
        out = _part_to_gemini(part, {})
        # When no clip / fps fields are set, video_metadata is omitted (None).
        assert out.video_metadata is None

    def test_tool_call_part(self) -> None:
        part = ToolCallPart(id="call_1", name="search", arguments={"q": "weather"})
        out = _part_to_gemini(part, {})
        assert out.function_call is not None
        assert out.function_call.id == "call_1"
        assert out.function_call.name == "search"
        assert out.function_call.args == {"q": "weather"}

    def test_tool_result_part_uses_name_lookup(self) -> None:
        lookup = {"call_1": "search"}
        part = ToolResultPart(id="call_1", output="sunny")
        out = _part_to_gemini(part, lookup)
        assert out.function_response is not None
        assert out.function_response.id == "call_1"
        assert out.function_response.name == "search"
        assert out.function_response.response == {"result": "sunny"}


class TestSystemConcatenation:
    def test_no_system_messages_returns_none(self) -> None:
        si, contents = _messages_to_gemini(
            [Message(role="user", parts=[TextPart(text="hi")])]
        )
        assert si is None
        assert len(contents) == 1
        assert contents[0].role == "user"

    def test_single_system_message(self) -> None:
        si, _ = _messages_to_gemini(
            [
                Message(role="system", parts=[TextPart(text="be terse")]),
                Message(role="user", parts=[TextPart(text="hi")]),
            ]
        )
        assert si == "be terse"

    def test_multiple_system_messages_joined_with_double_newline(self) -> None:
        si, _ = _messages_to_gemini(
            [
                Message(role="system", parts=[TextPart(text="be terse")]),
                Message(role="system", parts=[TextPart(text="answer in english")]),
                Message(role="user", parts=[TextPart(text="hi")]),
            ]
        )
        assert si == "be terse\n\nanswer in english"

    def test_system_with_non_text_part_raises(self) -> None:
        from pydantic import ValidationError

        # Try to construct, then if pydantic blocks it, use model_construct.
        try:
            msg = Message(
                role="system",
                parts=[ImagePart(url="https://example.com/img.png")],
            )
        except ValidationError:
            msg = Message.model_construct(
                role="system",
                parts=[ImagePart(url="https://example.com/img.png")],
            )
        with pytest.raises(UnsupportedContentError, match="TextPart"):
            _messages_to_gemini([msg])


class TestToolResultLookup:
    def test_tool_result_finds_name_from_prior_tool_call(self) -> None:
        si, contents = _messages_to_gemini(
            [
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
            ]
        )
        # Last content carries the function_response with name resolved
        last_part = contents[-1].parts[0]
        assert last_part.function_response.name == "get_weather"

    def test_tool_result_without_matching_call_raises(self) -> None:
        with pytest.raises(UnsupportedContentError, match="orphan_call"):
            _messages_to_gemini(
                [
                    Message(
                        role="tool",
                        parts=[ToolResultPart(id="orphan_call", output="x")],
                    )
                ]
            )

    def test_assistant_role_renamed_to_model(self) -> None:
        _, contents = _messages_to_gemini(
            [Message(role="assistant", parts=[TextPart(text="ok")])]
        )
        assert contents[0].role == "model"

    def test_tool_role_synthesised_as_user(self) -> None:
        _, contents = _messages_to_gemini(
            [
                Message(
                    role="assistant",
                    parts=[
                        ToolCallPart(id="c1", name="t", arguments={}),
                    ],
                ),
                Message(
                    role="tool",
                    parts=[ToolResultPart(id="c1", output="r")],
                ),
            ]
        )
        # The tool-role message becomes a user-role Content.
        assert contents[1].role == "user"
        assert contents[1].parts[0].function_response.name == "t"

    def test_tool_role_with_non_tool_result_raises(self) -> None:
        msg = Message.model_construct(
            role="tool", parts=[TextPart(text="oops")]
        )
        with pytest.raises(UnsupportedContentError, match="ToolResultPart"):
            _messages_to_gemini([msg])


from pydantic import BaseModel as PydanticBaseModel

from matrix.llm.gemini import (
    _build_sampling_kwargs,
    _extract_extended_kwargs,
    _response_format_to_gemini,
    _tool_choice_to_gemini,
    _tools_to_gemini,
)
from matrix.model.chat import Tool


class TestToolDefinitions:
    def test_no_tools_returns_empty_list(self) -> None:
        assert _tools_to_gemini(None) == []
        assert _tools_to_gemini([]) == []

    def test_single_tool_wraps_one_function_declaration(self) -> None:
        tool = Tool(
            id="search",
            description="Search the web",
            toolset_id="default",
            args_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        )
        out = _tools_to_gemini([tool])
        assert len(out) == 1
        assert len(out[0].function_declarations) == 1
        decl = out[0].function_declarations[0]
        assert decl.name == "search"
        assert decl.description == "Search the web"
        assert decl.parameters_json_schema == {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }

    def test_multiple_tools_fold_into_one_wrapper(self) -> None:
        tools = [
            Tool(id="a", description="A", toolset_id="t", args_schema={"type": "object"}),
            Tool(id="b", description="B", toolset_id="t", args_schema={"type": "object"}),
            Tool(id="c", description="C", toolset_id="t", args_schema={"type": "object"}),
        ]
        out = _tools_to_gemini(tools)
        assert len(out) == 1
        assert [d.name for d in out[0].function_declarations] == ["a", "b", "c"]


class TestToolChoice:
    def test_none_returns_none(self) -> None:
        assert _tool_choice_to_gemini(None) is None

    def test_auto_mode(self) -> None:
        from google.genai.types import FunctionCallingConfigMode

        tc = _tool_choice_to_gemini("auto")
        assert tc.function_calling_config.mode == FunctionCallingConfigMode.AUTO

    def test_required_maps_to_any(self) -> None:
        from google.genai.types import FunctionCallingConfigMode

        tc = _tool_choice_to_gemini("required")
        assert tc.function_calling_config.mode == FunctionCallingConfigMode.ANY

    def test_none_string_maps_to_none_mode(self) -> None:
        from google.genai.types import FunctionCallingConfigMode

        tc = _tool_choice_to_gemini("none")
        assert tc.function_calling_config.mode == FunctionCallingConfigMode.NONE

    def test_specific_tool_uses_any_with_allowed_function_names(self) -> None:
        from google.genai.types import FunctionCallingConfigMode

        tc = _tool_choice_to_gemini("get_weather")
        assert tc.function_calling_config.mode == FunctionCallingConfigMode.ANY
        assert tc.function_calling_config.allowed_function_names == ["get_weather"]


class TestResponseFormat:
    def test_none_returns_empty_dict(self) -> None:
        assert _response_format_to_gemini(None) == {}

    def test_dict_schema(self) -> None:
        schema = {"type": "object", "properties": {"v": {"type": "integer"}}}
        out = _response_format_to_gemini(schema)
        assert out == {
            "response_mime_type": "application/json",
            "response_schema": schema,
        }

    def test_pydantic_class(self) -> None:
        class Out(PydanticBaseModel):
            v: int

        out = _response_format_to_gemini(Out)
        assert out["response_mime_type"] == "application/json"
        assert "v" in out["response_schema"]["properties"]

    def test_invalid_type_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="response_format"):
            _response_format_to_gemini(42)  # type: ignore[arg-type]


class TestSampling:
    def test_all_params_forwarded(self) -> None:
        out = _build_sampling_kwargs(
            temperature=0.7, top_p=0.9, max_output_tokens=500, stop=["END"]
        )
        assert out == {
            "temperature": 0.7,
            "top_p": 0.9,
            "max_output_tokens": 500,
            "stop_sequences": ["END"],
        }

    def test_all_none_returns_empty(self) -> None:
        assert _build_sampling_kwargs(
            temperature=None, top_p=None, max_output_tokens=None, stop=None
        ) == {}

    def test_only_temperature_set(self) -> None:
        assert _build_sampling_kwargs(
            temperature=0.5, top_p=None, max_output_tokens=None, stop=None
        ) == {"temperature": 0.5}

    def test_only_stop_set(self) -> None:
        assert _build_sampling_kwargs(
            temperature=None, top_p=None, max_output_tokens=None, stop=["x", "y"]
        ) == {"stop_sequences": ["x", "y"]}


class TestExtendedKwargs:
    def test_none_returns_empty(self) -> None:
        assert _extract_extended_kwargs(None) == {}

    def test_empty_dict_returns_empty(self) -> None:
        assert _extract_extended_kwargs({}) == {}

    @pytest.mark.parametrize(
        "key, value",
        [
            ("top_k", 40),
            ("seed", 42),
            ("frequency_penalty", 0.5),
            ("presence_penalty", 0.5),
            ("safety_settings", []),
            ("response_logprobs", True),
            ("logprobs", 5),
        ],
    )
    def test_recognised_keys_passthrough(self, key: str, value) -> None:
        out = _extract_extended_kwargs({key: value})
        assert out == {key: value}

    def test_thinking_budget_folds_into_thinking_config(self) -> None:
        out = _extract_extended_kwargs({"thinking_budget": 2048})
        assert "thinking_config" in out
        assert out["thinking_config"].thinking_budget == 2048

    def test_include_thoughts_folds_into_thinking_config(self) -> None:
        out = _extract_extended_kwargs({"include_thoughts": True})
        assert "thinking_config" in out
        assert out["thinking_config"].include_thoughts is True

    def test_thinking_budget_and_include_thoughts_merge(self) -> None:
        out = _extract_extended_kwargs(
            {"thinking_budget": 1024, "include_thoughts": True}
        )
        assert out["thinking_config"].thinking_budget == 1024
        assert out["thinking_config"].include_thoughts is True

    def test_unknown_keys_dropped_with_debug_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="matrix.llm.gemini")
        out = _extract_extended_kwargs({"frobnicate": True, "foobar": 1})
        assert out == {}
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any(
            "frobnicate" in r.message and "foobar" in r.message
            for r in debug_records
        )


from matrix.llm.gemini import (
    _StreamState,
    _map_finish_reason,
    _translate_chunk,
)


class TestStopReason:
    def test_stop_no_function_call_maps_to_stop(self) -> None:
        assert _map_finish_reason("STOP", _StreamState()) == "stop"

    def test_stop_with_function_call_maps_to_tool_use(self) -> None:
        state = _StreamState()
        state.saw_function_call = True
        assert _map_finish_reason("STOP", state) == "tool_use"

    def test_max_tokens(self) -> None:
        assert _map_finish_reason("MAX_TOKENS", _StreamState()) == "max_tokens"

    def test_stop_sequence(self) -> None:
        assert _map_finish_reason("STOP_SEQUENCE", _StreamState()) == "stop_sequence"

    @pytest.mark.parametrize(
        "reason",
        ["SAFETY", "RECITATION", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "BLOCKLIST"],
    )
    def test_safety_family_maps_to_content_filter(self, reason: str) -> None:
        assert _map_finish_reason(reason, _StreamState()) == "content_filter"

    def test_malformed_function_call_maps_to_error(self) -> None:
        assert _map_finish_reason("MALFORMED_FUNCTION_CALL", _StreamState()) == "error"

    @pytest.mark.parametrize(
        "reason",
        ["LANGUAGE", "OTHER", "IMAGE_OTHER", "IMAGE_PROHIBITED",
         "UNEXPECTED_TOOL_CALL", "FINISH_REASON_UNSPECIFIED", "totally_made_up"],
    )
    def test_other_or_unknown_maps_to_other(self, reason: str) -> None:
        assert _map_finish_reason(reason, _StreamState()) == "other"


from types import SimpleNamespace as NS

from matrix.model.chat import (
    Citation,
    Done,
    ExtendedEvent,
    MediaDelta,
    ReasoningDelta,
    StreamStart,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)


class TestStreamMapping:
    def test_first_chunk_emits_stream_start(self) -> None:
        state = _StreamState()
        chunk = NS(response_id="r1", candidates=[NS(content=None, finish_reason=None)],
                   usage_metadata=None)
        out = _translate_chunk(chunk, state, model_name="gemini-2.5-flash")
        assert len(out) >= 1
        assert isinstance(out[0], StreamStart)
        assert out[0].request_id == "r1"
        assert out[0].model == "gemini-2.5-flash"

    def test_text_part_emits_text_delta(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        part = NS(text="hello", thought=None, function_call=None,
                  inline_data=None, executable_code=None, code_execution_result=None)
        chunk = NS(candidates=[NS(content=NS(parts=[part]), finish_reason=None,
                                  grounding_metadata=None, safety_ratings=None,
                                  logprobs_result=None)],
                   usage_metadata=None)
        out = _translate_chunk(chunk, state, model_name="gemini-2.5-flash")
        text_events = [e for e in out if isinstance(e, TextDelta)]
        assert len(text_events) == 1
        assert text_events[0].text == "hello"

    def test_thought_part_emits_reasoning_delta(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        part = NS(text="thinking...", thought=True, function_call=None,
                  inline_data=None, executable_code=None, code_execution_result=None)
        chunk = NS(candidates=[NS(content=NS(parts=[part]), finish_reason=None,
                                  grounding_metadata=None, safety_ratings=None,
                                  logprobs_result=None)],
                   usage_metadata=None)
        out = _translate_chunk(chunk, state, model_name="x")
        reasoning_events = [e for e in out if isinstance(e, ReasoningDelta)]
        assert len(reasoning_events) == 1
        assert reasoning_events[0].text == "thinking..."

    def test_function_call_atomic_start_delta_end(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        fc = NS(id="call_x", name="search", args={"q": "weather"})
        part = NS(text=None, thought=None, function_call=fc,
                  inline_data=None, executable_code=None, code_execution_result=None)
        chunk = NS(candidates=[NS(content=NS(parts=[part]), finish_reason=None,
                                  grounding_metadata=None, safety_ratings=None,
                                  logprobs_result=None)],
                   usage_metadata=None)
        out = _translate_chunk(chunk, state, model_name="x")
        kinds = [type(e).__name__ for e in out]
        assert "ToolCallStart" in kinds
        assert "ToolCallDelta" in kinds
        assert "ToolCallEnd" in kinds
        assert state.saw_function_call is True

    def test_function_call_synthesises_id_when_absent(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        fc = NS(id=None, name="search", args={"q": "x"})
        part = NS(text=None, thought=None, function_call=fc,
                  inline_data=None, executable_code=None, code_execution_result=None)
        chunk = NS(candidates=[NS(content=NS(parts=[part]), finish_reason=None,
                                  grounding_metadata=None, safety_ratings=None,
                                  logprobs_result=None)],
                   usage_metadata=None)
        out = _translate_chunk(chunk, state, model_name="x")
        starts = [e for e in out if isinstance(e, ToolCallStart)]
        assert len(starts) == 1
        assert starts[0].id.startswith("call_")

    def test_audio_inline_data_emits_audio_media_delta(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        part = NS(text=None, thought=None, function_call=None,
                  inline_data=NS(mime_type="audio/wav", data=b"wav"),
                  executable_code=None, code_execution_result=None)
        chunk = NS(candidates=[NS(content=NS(parts=[part]), finish_reason=None,
                                  grounding_metadata=None, safety_ratings=None,
                                  logprobs_result=None)],
                   usage_metadata=None)
        out = _translate_chunk(chunk, state, model_name="x")
        media = [e for e in out if isinstance(e, MediaDelta)]
        assert len(media) == 1
        assert media[0].kind == "audio"
        assert media[0].data == b"wav"

    def test_image_inline_data_emits_image_media_delta(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        part = NS(text=None, thought=None, function_call=None,
                  inline_data=NS(mime_type="image/png", data=b"png"),
                  executable_code=None, code_execution_result=None)
        chunk = NS(candidates=[NS(content=NS(parts=[part]), finish_reason=None,
                                  grounding_metadata=None, safety_ratings=None,
                                  logprobs_result=None)],
                   usage_metadata=None)
        out = _translate_chunk(chunk, state, model_name="x")
        media = [e for e in out if isinstance(e, MediaDelta)]
        assert len(media) == 1
        assert media[0].kind == "image"

    def test_grounding_metadata_emits_citations(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        gc = NS(web=NS(uri="https://example.com", title="Example"),
                retrieved_context=None)
        chunk = NS(candidates=[NS(content=NS(parts=[]), finish_reason=None,
                                  grounding_metadata=NS(grounding_chunks=[gc]),
                                  safety_ratings=None, logprobs_result=None)],
                   usage_metadata=None)
        out = _translate_chunk(chunk, state, model_name="x")
        citations = [e for e in out if isinstance(e, ExtendedEvent)
                     and isinstance(e.extended, Citation)]
        assert len(citations) == 1
        assert citations[0].extended.source_url == "https://example.com"

    def test_final_chunk_emits_usage_then_done(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        usage = NS(prompt_token_count=10, candidates_token_count=5,
                   cached_content_token_count=2, thoughts_token_count=1)
        chunk = NS(candidates=[NS(content=NS(parts=[]), finish_reason="STOP",
                                  grounding_metadata=None, safety_ratings=None,
                                  logprobs_result=None)],
                   usage_metadata=usage, response_id=None)
        out = _translate_chunk(chunk, state, model_name="x")
        kinds = [type(e).__name__ for e in out]
        assert "Usage" in kinds
        assert "Done" in kinds
        # Usage should come before Done
        assert kinds.index("Usage") < kinds.index("Done")
        usage_event = [e for e in out if isinstance(e, Usage)][0]
        assert usage_event.input_tokens == 10
        assert usage_event.output_tokens == 5
        assert usage_event.cached_input_tokens == 2
        assert usage_event.reasoning_tokens == 1
        assert usage_event.cumulative is True

    def test_final_chunk_without_usage_still_emits_done(self) -> None:
        state = _StreamState()
        state.emitted_stream_start = True
        chunk = NS(candidates=[NS(content=NS(parts=[]), finish_reason="STOP",
                                  grounding_metadata=None, safety_ratings=None,
                                  logprobs_result=None)],
                   usage_metadata=None, response_id=None)
        out = _translate_chunk(chunk, state, model_name="x")
        done_events = [e for e in out if isinstance(e, Done)]
        assert len(done_events) == 1

    def test_finish_reason_enum_with_name_attr(self) -> None:
        # FinishReason might be an enum with .name; the translator should use it.
        state = _StreamState()
        state.emitted_stream_start = True
        finish = NS(name="MAX_TOKENS")
        chunk = NS(candidates=[NS(content=NS(parts=[]), finish_reason=finish,
                                  grounding_metadata=None, safety_ratings=None,
                                  logprobs_result=None)],
                   usage_metadata=None, response_id=None)
        out = _translate_chunk(chunk, state, model_name="x")
        done = [e for e in out if isinstance(e, Done)][0]
        assert done.stop_reason == "max_tokens"
        assert done.raw_reason == "MAX_TOKENS"


from collections.abc import AsyncIterator

from google.genai import errors as gerrors

from matrix.model.except_ import (
    AuthenticationError,
    ModelNotFoundError,
    RateLimitError,
)


async def _aiter(items: list) -> AsyncIterator:
    for item in items:
        yield item


def _patched_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the genai.Client symbol inside the adapter module."""
    mock_instance = MagicMock()
    mock_instance.aio = MagicMock()
    mock_instance.aio.models = MagicMock()
    mock_instance.aio.models.generate_content_stream = AsyncMock()
    cls_mock = MagicMock(return_value=mock_instance)
    monkeypatch.setattr("matrix.llm.gemini.genai.Client", cls_mock)
    return mock_instance


def _make_api_error(code: int, *, message: str = "test failure") -> gerrors.APIError:
    """Build a google-genai APIError using the SDK's actual constructor."""
    return gerrors.APIError(
        code,
        {"error": {"code": code, "message": message, "status": "TEST_STATUS"}},
    )


def _ok_chunks(*, model: str = "gemini-2.5-flash"):
    """Build a minimal stream of mock GenerateContentResponse chunks."""
    return [
        NS(
            response_id="r1",
            candidates=[
                NS(
                    content=NS(
                        parts=[
                            NS(text="hi", thought=None, function_call=None,
                               inline_data=None, executable_code=None,
                               code_execution_result=None)
                        ]
                    ),
                    finish_reason=None,
                    grounding_metadata=None,
                    safety_ratings=None,
                    logprobs_result=None,
                )
            ],
            usage_metadata=None,
        ),
        NS(
            response_id=None,
            candidates=[
                NS(
                    content=NS(parts=[]),
                    finish_reason="STOP",
                    grounding_metadata=None,
                    safety_ratings=None,
                    logprobs_result=None,
                )
            ],
            usage_metadata=NS(
                prompt_token_count=2,
                candidates_token_count=1,
                cached_content_token_count=None,
                thoughts_token_count=None,
            ),
        ),
    ]


class TestStream:
    async def test_unknown_model_raises_model_not_found(self) -> None:
        provider = _make_provider(models=["gemini-2.5-flash"])
        llm = GeminiLLM(provider)
        with pytest.raises(ModelNotFoundError, match="not-a-model"):
            async for _ in llm.stream(
                model="not-a-model",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_full_stream_emits_start_text_usage_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = GeminiLLM(provider)
        client = _patched_client(monkeypatch)
        client.aio.models.generate_content_stream.return_value = _aiter(_ok_chunks())

        out = [
            ev
            async for ev in llm.stream(
                model="gemini-2.5-flash",
                messages=[Message(role="user", parts=[TextPart(text="hello")])],
            )
        ]
        kinds = [type(e).__name__ for e in out]
        assert "StreamStart" in kinds
        assert "TextDelta" in kinds
        assert "Usage" in kinds
        assert kinds[-1] == "Done"

    async def test_request_payload_has_correct_model_and_contents(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = GeminiLLM(provider)
        client = _patched_client(monkeypatch)
        client.aio.models.generate_content_stream.return_value = _aiter(_ok_chunks())
        async for _ in llm.stream(
            model="gemini-2.5-flash",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
        ):
            pass
        kwargs = client.aio.models.generate_content_stream.call_args.kwargs
        assert kwargs["model"] == "gemini-2.5-flash"
        assert len(kwargs["contents"]) == 1
        assert kwargs["contents"][0].role == "user"

    async def test_request_payload_includes_tools_when_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = GeminiLLM(provider)
        client = _patched_client(monkeypatch)
        client.aio.models.generate_content_stream.return_value = _aiter(_ok_chunks())
        tool = Tool(
            id="search", description="Search", toolset_id="t",
            args_schema={"type": "object", "properties": {}, "required": []},
        )
        async for _ in llm.stream(
            model="gemini-2.5-flash",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            tools=[tool],
            tool_choice="auto",
        ):
            pass
        kwargs = client.aio.models.generate_content_stream.call_args.kwargs
        config = kwargs["config"]
        assert len(config.tools) == 1
        assert config.tool_config is not None

    async def test_system_message_lifted_to_system_instruction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = GeminiLLM(provider)
        client = _patched_client(monkeypatch)
        client.aio.models.generate_content_stream.return_value = _aiter(_ok_chunks())
        async for _ in llm.stream(
            model="gemini-2.5-flash",
            messages=[
                Message(role="system", parts=[TextPart(text="be terse")]),
                Message(role="user", parts=[TextPart(text="hi")]),
            ],
        ):
            pass
        config = client.aio.models.generate_content_stream.call_args.kwargs["config"]
        assert config.system_instruction == "be terse"


class TestExceptionWrapping:
    async def test_pre_stream_exception_re_raised_as_matrix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = GeminiLLM(provider)
        client = _patched_client(monkeypatch)
        client.aio.models.generate_content_stream.side_effect = _make_api_error(401, message="auth fail")
        with pytest.raises(AuthenticationError):
            async for _ in llm.stream(
                model="gemini-2.5-flash",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_mid_stream_exception_yields_terminal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        llm = GeminiLLM(provider)
        client = _patched_client(monkeypatch)

        async def failing_iter() -> AsyncIterator:
            yield NS(
                response_id="r1",
                candidates=[
                    NS(content=NS(parts=[]), finish_reason=None,
                       grounding_metadata=None, safety_ratings=None,
                       logprobs_result=None)
                ],
                usage_metadata=None,
            )
            raise _make_api_error(429, message="rate limit")

        client.aio.models.generate_content_stream.return_value = failing_iter()
        events = [
            ev
            async for ev in llm.stream(
                model="gemini-2.5-flash",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            )
        ]
        from matrix.model.chat import Error as ChatError

        assert isinstance(events[-1], ChatError)
        assert events[-1].fatal is True
        assert isinstance(events[0], StreamStart)


class TestConcurrency:
    async def test_semaphore_serialises_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider(max_concurrency=1)
        llm = GeminiLLM(provider)
        client = _patched_client(monkeypatch)

        in_flight = 0
        peak = 0

        async def slow_iter() -> AsyncIterator:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            yield NS(
                response_id=None,
                candidates=[
                    NS(content=NS(parts=[]), finish_reason="STOP",
                       grounding_metadata=None, safety_ratings=None,
                       logprobs_result=None)
                ],
                usage_metadata=None,
            )
            in_flight -= 1

        client.aio.models.generate_content_stream.side_effect = lambda **_: slow_iter()

        async def consume() -> None:
            async for _ in llm.stream(
                model="gemini-2.5-flash",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

        await asyncio.gather(consume(), consume(), consume())
        assert peak == 1


class TestPackageReexport:
    def test_gemini_llm_reexported_from_package(self) -> None:
        import matrix.llm as llm_pkg

        assert "GeminiLLM" in llm_pkg.__all__
        assert llm_pkg.GeminiLLM is GeminiLLM

    def test_openresponses_llm_still_reexported(self) -> None:
        import matrix.llm as llm_pkg

        assert "OpenResponsesLLM" in llm_pkg.__all__
