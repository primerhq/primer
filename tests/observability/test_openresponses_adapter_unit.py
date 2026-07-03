"""Unit tests for the OpenResponses LLM adapter
(:mod:`primer.llm.openresponses`).

Placed under ``tests/observability`` (an included directory) because the
CI coverage sweep ignores ``tests/llm``. Pure translators are called
directly; streaming events are faked with ``SimpleNamespace``; the
``AsyncOpenAI`` client is patched at the module symbol so ``stream()``
can be driven end-to-end.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, HttpUrl, SecretStr

from primer.llm.openresponses import (
    OpenResponsesLLM,
    _FlavorPolicy,
    _POLICY_BY_FLAVOR,
    _StreamState,
    _annotation_to_citation,
    _build_sampling_params,
    _build_usage,
    _extract_extended_kwargs,
    _map_incomplete_reason,
    _map_stop_reason,
    _messages_to_input_items,
    _part_to_input_content,
    _response_format_to_text_param,
    _tool_choice_to_openai,
    _tool_to_openai,
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
    MediaDelta,
    Message,
    RawReasoningDelta,
    ReasoningDelta,
    RefusalDelta,
    ServerToolCallDelta,
    ServerToolCallEnd,
    ServerToolCallStart,
    StreamStart,
    TextDelta,
    TextPart,
    Tool,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallPart,
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
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenResponsesConfig,
    OpenResponsesFlavor,
)


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
            LLMModel(name=name, context_length=128_000)
            for name in (models or ["gpt-4o-mini"])
        ],
        config=OpenResponsesConfig(
            url=HttpUrl("https://api.openai.com/v1/"),
            api_key=SecretStr(api_key),
            flavor=flavor,
        ),
        limits=Limits(max_concurrency=max_concurrency),
    )


async def _aiter(items: list) -> AsyncIterator:
    for item in items:
        yield item


# --------------------------------------------------------------------------- #
# Part -> input content mapping                                               #
# --------------------------------------------------------------------------- #


class TestPartToInputContent:
    def test_text_user_input_text(self) -> None:
        assert _part_to_input_content(TextPart(text="hi"), role="user") == {
            "type": "input_text",
            "text": "hi",
        }

    def test_text_assistant_output_text(self) -> None:
        assert _part_to_input_content(TextPart(text="hi"), role="assistant") == {
            "type": "output_text",
            "text": "hi",
        }

    def test_image_data_becomes_data_uri(self) -> None:
        out = _part_to_input_content(ImagePart(data=b"img", mime_type="image/png"))
        assert out["type"] == "input_image"
        assert out["image_url"].startswith("data:image/png;base64,")

    def test_image_data_default_mime(self) -> None:
        out = _part_to_input_content(ImagePart(data=b"img"))
        assert out["image_url"].startswith("data:application/octet-stream;base64,")

    def test_image_url_and_detail(self) -> None:
        out = _part_to_input_content(ImagePart(url="https://x/i.png", detail="high"))
        assert out["image_url"] == "https://x/i.png"
        assert out["detail"] == "high"

    def test_image_file_id(self) -> None:
        out = _part_to_input_content(ImagePart(file_id="file_1"))
        assert out["file_id"] == "file_1"

    def test_document_data_uri_and_filename(self) -> None:
        out = _part_to_input_content(
            DocumentPart(data=b"%PDF", mime_type="application/pdf", filename="r.pdf")
        )
        assert out["type"] == "input_file"
        assert out["file_data"].startswith("data:application/pdf;base64,")
        assert out["filename"] == "r.pdf"

    def test_document_data_default_filename(self) -> None:
        out = _part_to_input_content(DocumentPart(data=b"%PDF"))
        assert out["filename"] == "file"

    def test_document_url(self) -> None:
        out = _part_to_input_content(DocumentPart(url="https://x/d.pdf", filename="d.pdf"))
        assert out["file_url"] == "https://x/d.pdf"
        assert out["filename"] == "d.pdf"

    def test_document_file_id(self) -> None:
        out = _part_to_input_content(DocumentPart(file_id="file_9", filename="n"))
        assert out["file_id"] == "file_9"
        assert out["filename"] == "n"

    def test_audio_mp3(self) -> None:
        out = _part_to_input_content(
            ExtendedPart(extended=AudioPart(data=b"a", mime_type="audio/mpeg"))
        )
        assert out["type"] == "input_audio"
        assert out["input_audio"]["format"] == "mp3"
        assert base64.b64decode(out["input_audio"]["data"]) == b"a"

    def test_audio_wav(self) -> None:
        out = _part_to_input_content(
            ExtendedPart(extended=AudioPart(data=b"a", mime_type="audio/wav"))
        )
        assert out["input_audio"]["format"] == "wav"

    def test_audio_without_data_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="inline base64 audio"):
            _part_to_input_content(
                ExtendedPart(extended=AudioPart(url="https://x/a.mp3", mime_type="audio/mpeg"))
            )

    def test_audio_bad_mime_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="audio/mp3"):
            _part_to_input_content(
                ExtendedPart(extended=AudioPart(data=b"a", mime_type="audio/ogg"))
            )

    def test_video_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="does not accept video"):
            _part_to_input_content(ExtendedPart(extended=VideoPart(data=b"v")))


class TestMessagesToInputItems:
    def test_system_and_user_inline(self) -> None:
        items = _messages_to_input_items(
            [
                Message(role="system", parts=[TextPart(text="be terse")]),
                Message(role="user", parts=[TextPart(text="hi")]),
            ]
        )
        assert items[0]["role"] == "system"
        assert items[0]["content"][0] == {"type": "input_text", "text": "be terse"}
        assert items[1]["role"] == "user"

    def test_tool_role_becomes_function_call_output(self) -> None:
        items = _messages_to_input_items(
            [Message(role="tool", parts=[ToolResultPart(id="call_1", output="42")])]
        )
        assert items == [
            {"type": "function_call_output", "call_id": "call_1", "output": "42"}
        ]

    def test_tool_role_non_tool_result_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="tool-role"):
            _messages_to_input_items(
                [Message(role="tool", parts=[TextPart(text="x")])]
            )

    def test_assistant_tool_call_splits_out(self) -> None:
        items = _messages_to_input_items(
            [
                Message(
                    role="assistant",
                    parts=[
                        TextPart(text="calling"),
                        ToolCallPart(id="call_1", name="search", arguments={"q": "x"}),
                    ],
                )
            ]
        )
        # First a message item flushed, then the function_call item.
        assert items[0]["role"] == "assistant"
        assert items[0]["content"][0]["type"] == "output_text"
        assert items[1]["type"] == "function_call"
        assert items[1]["call_id"] == "call_1"
        assert items[1]["name"] == "search"

    def test_tool_result_outside_tool_role_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="only valid inside a tool-role"):
            _messages_to_input_items(
                [Message(role="user", parts=[ToolResultPart(id="c", output="o")])]
            )


class _Schema(BaseModel):
    answer: str


class TestToolAndFormatTranslation:
    def test_tool_to_openai(self) -> None:
        tool = Tool(id="search", description="S", toolset_id="t", args_schema={"type": "object"})
        assert _tool_to_openai(tool) == {
            "type": "function",
            "name": "search",
            "description": "S",
            "parameters": {"type": "object"},
        }

    def test_tool_choice_variants(self) -> None:
        assert _tool_choice_to_openai(None) is None
        assert _tool_choice_to_openai("auto") == "auto"
        assert _tool_choice_to_openai("required") == "required"
        assert _tool_choice_to_openai("none") == "none"
        assert _tool_choice_to_openai("search") == {"type": "function", "name": "search"}

    def test_response_format_none(self) -> None:
        assert _response_format_to_text_param(None) is None

    def test_response_format_dict(self) -> None:
        out = _response_format_to_text_param({"type": "object"})
        assert out["format"]["name"] == "schema"
        assert out["format"]["schema"] == {"type": "object"}
        assert out["format"]["strict"] is True

    def test_response_format_pydantic(self) -> None:
        out = _response_format_to_text_param(_Schema)
        assert out["format"]["name"] == "_Schema"
        assert out["format"]["schema"]["properties"]["answer"]["type"] == "string"

    def test_response_format_invalid(self) -> None:
        with pytest.raises(ConfigError, match="Pydantic class or dict"):
            _response_format_to_text_param(123)  # type: ignore[arg-type]


class TestSamplingAndExtended:
    def test_max_output_tokens_key(self) -> None:
        out = _build_sampling_params(
            temperature=0.5, top_p=0.9, max_output_tokens=64, stop=None
        )
        assert out["temperature"] == 0.5
        assert out["top_p"] == 0.9
        assert out["max_output_tokens"] == 64

    def test_stop_is_dropped_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING)
        out = _build_sampling_params(
            temperature=None, top_p=None, max_output_tokens=None, stop=["END"]
        )
        assert "stop" not in out
        assert any("stop" in r.message.lower() for r in caplog.records)

    def test_extended_reasoning_and_passthrough(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.openresponses")
        out = _extract_extended_kwargs(
            {
                "reasoning_effort": "high",
                "reasoning_summary": "auto",
                "parallel_tool_calls": True,
                "bogus": 1,
            }
        )
        assert out["reasoning"] == {"effort": "high", "summary": "auto"}
        assert out["parallel_tool_calls"] is True
        assert any("dropped unknown" in r.message for r in caplog.records)

    def test_extended_empty(self) -> None:
        assert _extract_extended_kwargs(None) == {}
        assert _extract_extended_kwargs({}) == {}


class TestStopAndUsage:
    def test_map_stop_reason_completed_stop(self) -> None:
        assert _map_stop_reason("completed", _StreamState()) == "stop"

    def test_map_stop_reason_completed_tool_use(self) -> None:
        assert _map_stop_reason("completed", _StreamState(saw_function_call=True)) == "tool_use"

    def test_map_stop_reason_failed(self) -> None:
        assert _map_stop_reason("failed", _StreamState()) == "error"

    def test_map_stop_reason_other(self) -> None:
        assert _map_stop_reason("weird", _StreamState()) == "other"

    def test_map_incomplete_reason(self) -> None:
        assert _map_incomplete_reason("max_output_tokens") == "max_tokens"
        assert _map_incomplete_reason("content_filter") == "content_filter"
        assert _map_incomplete_reason(None) == "other"

    def test_build_usage_none(self) -> None:
        assert _build_usage(None) is None

    def test_build_usage_missing_counts(self) -> None:
        assert _build_usage(NS(input_tokens=None, output_tokens=5)) is None

    def test_build_usage_full_with_details(self) -> None:
        usage = _build_usage(
            NS(
                input_tokens=10,
                output_tokens=20,
                input_tokens_details=NS(cached_tokens=3),
                output_tokens_details=NS(reasoning_tokens=6),
            )
        )
        assert usage == Usage(
            input_tokens=10,
            output_tokens=20,
            cached_input_tokens=3,
            reasoning_tokens=6,
            cumulative=False,
        )

    def test_annotation_to_citation(self) -> None:
        cit = _annotation_to_citation(
            NS(url="https://x", title="T", start_index=1, end_index=9), index=2
        )
        assert cit.source_url == "https://x"
        assert cit.index == 2


class TestFlavorPolicy:
    def test_openai_requires_key(self) -> None:
        assert _POLICY_BY_FLAVOR[OpenResponsesFlavor.OPENAI].require_api_key is True

    def test_lmstudio_tolerates_no_key(self) -> None:
        p = _POLICY_BY_FLAVOR[OpenResponsesFlavor.LMSTUDIO]
        assert p.require_api_key is False
        assert p.drop_encrypted_reasoning is True

    def test_policy_frozen(self) -> None:
        with pytest.raises(Exception):
            _POLICY_BY_FLAVOR[OpenResponsesFlavor.OPENAI].require_api_key = False  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Stream-event translation                                                    #
# --------------------------------------------------------------------------- #


class TestTranslateEvent:
    def test_response_created(self) -> None:
        out = _translate_event(
            NS(type="response.created", response=NS(id="resp_1", model="gpt-4o-mini")),
            _StreamState(),
        )
        assert out == [StreamStart(request_id="resp_1", model="gpt-4o-mini")]

    def test_output_item_added_message(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(type="response.output_item.added", item=NS(type="message", id="m1")),
            state,
        )
        assert out == []
        assert state.item_kind["m1"] == "message"

    def test_output_item_added_function_call(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(
                type="response.output_item.added",
                item=NS(type="function_call", id="i1", call_id="call_1", name="search"),
            ),
            state,
        )
        assert out[0].id == "call_1"
        assert out[0].name == "search"
        assert state.saw_function_call is True
        assert state.item_call_id["i1"] == "call_1"

    def test_output_item_added_server_tool(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(type="response.output_item.added", item=NS(type="web_search_call", id="w1")),
            state,
        )
        assert isinstance(out[0].extended, ServerToolCallStart)
        assert out[0].extended.tool_name == "web_search"

    def test_output_item_added_unknown(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(type="response.output_item.added", item=NS(type="mystery", id="x1")),
            state,
        )
        assert out == []
        assert state.item_kind["x1"] == "mystery"

    def test_content_part_added(self) -> None:
        out = _translate_event(
            NS(type="response.content_part.added", item_id="m1", content_index=0),
            _StreamState(),
        )
        assert out == []

    def test_output_text_delta(self) -> None:
        out = _translate_event(
            NS(type="response.output_text.delta", item_id="m1", content_index=0, delta="hi"),
            _StreamState(),
        )
        assert out == [TextDelta(text="hi", index=0)]

    def test_reasoning_summary_delta(self) -> None:
        out = _translate_event(
            NS(
                type="response.reasoning_summary_text.delta",
                item_id="m1",
                summary_index=0,
                delta="thinking",
            ),
            _StreamState(),
        )
        assert out == [ReasoningDelta(text="thinking", index=0)]

    def test_reasoning_text_delta(self) -> None:
        out = _translate_event(
            NS(type="response.reasoning_text.delta", item_id="m1", content_index=0, delta="raw"),
            _StreamState(),
        )
        assert isinstance(out[0].extended, RawReasoningDelta)
        assert out[0].extended.text == "raw"

    def test_refusal_delta(self) -> None:
        out = _translate_event(
            NS(type="response.refusal.delta", item_id="m1", content_index=0, delta="no"),
            _StreamState(),
        )
        assert isinstance(out[0].extended, RefusalDelta)
        assert out[0].extended.text == "no"

    def test_function_call_arguments_delta_and_done(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(
                type="response.output_item.added",
                item=NS(type="function_call", id="i1", call_id="call_1", name="search"),
            ),
            state,
        )
        delta = _translate_event(
            NS(type="response.function_call_arguments.delta", item_id="i1", delta='{"q":'),
            state,
        )
        assert isinstance(delta[0], ToolCallDelta)
        assert delta[0].id == "call_1"
        done = _translate_event(
            NS(type="response.function_call_arguments.done", item_id="i1", arguments='{"q": "x"}'),
            state,
        )
        assert done == [ToolCallEnd(id="call_1", arguments={"q": "x"}, index=done[0].index)]

    def test_function_call_arguments_done_bad_json(self) -> None:
        state = _StreamState()
        out = _translate_event(
            NS(type="response.function_call_arguments.done", item_id="i9", arguments="{bad"),
            state,
        )
        assert out[0].arguments == {}

    def test_audio_delta(self) -> None:
        raw = base64.b64encode(b"snd").decode()
        out = _translate_event(
            NS(type="response.audio.delta", item_id="m1", content_index=0, delta=raw),
            _StreamState(),
        )
        assert isinstance(out[0], MediaDelta)
        assert out[0].kind == "audio"
        assert out[0].data == b"snd"

    def test_image_partial(self) -> None:
        raw = base64.b64encode(b"img").decode()
        out = _translate_event(
            NS(type="response.image_generation_call.partial_image", item_id="m1", partial_image_b64=raw),
            _StreamState(),
        )
        assert out[0].kind == "image"
        assert out[0].data == b"img"

    def test_code_interpreter_delta(self) -> None:
        out = _translate_event(
            NS(type="response.code_interpreter_call.code.delta", item_id="c1", delta="print(1)"),
            _StreamState(),
        )
        assert isinstance(out[0].extended, ServerToolCallDelta)
        assert out[0].extended.text == "print(1)"

    def test_annotation_added(self) -> None:
        out = _translate_event(
            NS(
                type="response.output_text_annotation.added",
                item_id="m1",
                content_index=0,
                annotation=NS(url="https://x", title="T"),
            ),
            _StreamState(),
        )
        assert isinstance(out[0].extended, Citation)

    def test_annotation_added_missing(self) -> None:
        out = _translate_event(
            NS(type="response.output_text_annotation.added", item_id="m1", content_index=0, annotation=None),
            _StreamState(),
        )
        assert out == []

    def test_response_completed_with_usage(self) -> None:
        out = _translate_event(
            NS(
                type="response.completed",
                response=NS(
                    usage=NS(
                        input_tokens=5,
                        output_tokens=7,
                        input_tokens_details=None,
                        output_tokens_details=None,
                    )
                ),
            ),
            _StreamState(),
        )
        assert isinstance(out[0], Usage)
        assert isinstance(out[1], Done)
        assert out[1].stop_reason == "stop"

    def test_response_completed_without_usage(self) -> None:
        out = _translate_event(
            NS(type="response.completed", response=NS(usage=None)), _StreamState()
        )
        assert len(out) == 1
        assert isinstance(out[0], Done)

    def test_response_failed(self) -> None:
        out = _translate_event(NS(type="response.failed"), _StreamState())
        assert out == [Done(stop_reason="error", raw_reason="failed")]

    def test_response_incomplete_max_tokens(self) -> None:
        out = _translate_event(
            NS(
                type="response.incomplete",
                response=NS(incomplete_details=NS(reason="max_output_tokens")),
            ),
            _StreamState(),
        )
        assert out[0].stop_reason == "max_tokens"
        assert out[0].raw_reason == "incomplete:max_output_tokens"

    def test_response_incomplete_no_reason(self) -> None:
        out = _translate_event(
            NS(type="response.incomplete", response=NS(incomplete_details=NS(reason=None))),
            _StreamState(),
        )
        assert out[0].raw_reason == "incomplete"

    def test_error_event(self) -> None:
        out = _translate_event(
            NS(type="error", code="rate_limit", message="slow down"), _StreamState()
        )
        assert isinstance(out[0], ChatError)
        assert out[0].fatal is False
        assert out[0].code == "rate_limit"

    def test_generic_server_tool_completed_ends_call(self) -> None:
        state = _StreamState()
        _translate_event(
            NS(type="response.output_item.added", item=NS(type="web_search_call", id="w1")),
            state,
        )
        out = _translate_event(
            NS(type="response.web_search_call.completed", item_id="w1"), state
        )
        assert isinstance(out[0].extended, ServerToolCallEnd)

    def test_generic_done_in_skip_list(self) -> None:
        assert _translate_event(
            NS(type="response.output_text.done", item_id="m1"), _StreamState()
        ) == []

    def test_generic_completed_non_server_tool_is_noop(self) -> None:
        # An item that is not a tracked server-tool call yields nothing.
        assert _translate_event(
            NS(type="response.custom_thing.completed", item_id="unknown"),
            _StreamState(),
        ) == []

    def test_unhandled_event(self) -> None:
        assert _translate_event(NS(type="response.mystery.thing"), _StreamState()) == []


# --------------------------------------------------------------------------- #
# stream() full-drive                                                         #
# --------------------------------------------------------------------------- #


def _patched_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    inst = MagicMock()
    inst.responses = MagicMock()
    inst.responses.create = AsyncMock()
    monkeypatch.setattr("primer.llm.openresponses.AsyncOpenAI", MagicMock(return_value=inst))
    return inst


def _ok_events() -> list[Any]:
    return [
        NS(type="response.created", response=NS(id="resp_1", model="gpt-4o-mini")),
        NS(type="response.output_item.added", item=NS(type="message", id="m1")),
        NS(type="response.output_text.delta", item_id="m1", content_index=0, delta="hi"),
        NS(
            type="response.completed",
            response=NS(
                usage=NS(
                    input_tokens=5,
                    output_tokens=7,
                    input_tokens_details=None,
                    output_tokens_details=None,
                )
            ),
        ),
    ]


class TestStream:
    async def test_unknown_model_raises(self) -> None:
        llm = OpenResponsesLLM(_make_provider(models=["gpt-4o-mini"]))
        with pytest.raises(ModelNotFoundError, match="not-real"):
            async for _ in llm.stream(
                model="not-real",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_full_stream_sequence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenResponsesLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.responses.create.return_value = _aiter(_ok_events())
        out = [
            ev
            async for ev in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            )
        ]
        assert [type(e).__name__ for e in out] == ["StreamStart", "TextDelta", "Usage", "Done"]

    async def test_request_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenResponsesLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.responses.create.return_value = _aiter(_ok_events())
        tool = Tool(id="search", description="S", toolset_id="t", args_schema={"type": "object"})
        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            temperature=0.3,
            max_output_tokens=64,
            tools=[tool],
            tool_choice="required",
            extended={"parallel_tool_calls": True, "junk": 1},
        ):
            pass
        kwargs = client.responses.create.call_args.kwargs
        assert kwargs["model"] == "gpt-4o-mini"
        assert kwargs["store"] is False
        assert kwargs["stream"] is True
        assert kwargs["temperature"] == 0.3
        assert kwargs["max_output_tokens"] == 64
        assert kwargs["tools"][0]["name"] == "search"
        assert kwargs["tool_choice"] == "required"
        assert kwargs["parallel_tool_calls"] is True
        assert "junk" not in kwargs

    async def test_response_format_in_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenResponsesLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.responses.create.return_value = _aiter(_ok_events())
        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            response_format=_Schema,
        ):
            pass
        kwargs = client.responses.create.call_args.kwargs
        assert kwargs["text"]["format"]["name"] == "_Schema"

    async def test_error_before_stream_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenResponsesLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.responses.create = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(PrimerError):
            async for _ in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_mid_stream_error_yields_chat_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = OpenResponsesLLM(_make_provider())
        client = _patched_client(monkeypatch)

        async def _failing() -> AsyncIterator:
            yield NS(type="response.created", response=NS(id="r", model="gpt-4o-mini"))
            raise RuntimeError("mid boom")

        client.responses.create.return_value = _failing()
        out = [
            ev
            async for ev in llm.stream(
                model="gpt-4o-mini",
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

    async def aclose(self) -> None:
        return None


async def _slow_forever(event: Any) -> AsyncIterator:
    while True:
        await asyncio.sleep(0.005)
        yield event


class TestTimeouts:
    async def test_connect_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from primer.model.except_ import ProviderTimeoutError

        llm = OpenResponsesLLM(_make_provider())
        llm._connect_timeout_seconds = 0.05
        client = _patched_client(monkeypatch)

        async def _stall(**_: Any) -> Any:
            await asyncio.sleep(3600)

        client.responses.create = _stall
        with pytest.raises(ProviderTimeoutError) as exc:
            async for _ in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
        assert exc.value.code == "connect_timeout"

    async def test_stream_stall_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from primer.model.except_ import ProviderTimeoutError

        llm = OpenResponsesLLM(_make_provider())
        llm._connect_timeout_seconds = None
        llm._request_timeout_seconds = 0.05
        client = _patched_client(monkeypatch)
        client.responses.create.return_value = _StallStream()
        with pytest.raises(ProviderTimeoutError) as exc:
            async for _ in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
        assert exc.value.code == "stream_timeout"

    async def test_generation_budget_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from primer.model.except_ import ProviderTimeoutError

        llm = OpenResponsesLLM(_make_provider())
        llm._connect_timeout_seconds = None
        llm._request_timeout_seconds = None
        llm._total_timeout_seconds = 0.05
        client = _patched_client(monkeypatch)
        client.responses.create.return_value = _slow_forever(NS(type="response.in_progress"))
        with pytest.raises(ProviderTimeoutError) as exc:
            async for _ in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
        assert exc.value.code == "generation_timeout"

    async def test_trace_llm_io(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenResponsesLLM(_make_provider(), trace_llm_io=True)
        client = _patched_client(monkeypatch)
        client.responses.create.return_value = _aiter(_ok_events())
        out = [
            ev
            async for ev in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            )
        ]
        assert any(type(e).__name__ == "Done" for e in out)


class TestAdapterLifecycle:
    def test_rejects_wrong_provider_type(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", LLMProviderType.ANTHROPIC)
        with pytest.raises(ConfigError, match="OPENRESPONSES"):
            OpenResponsesLLM(provider)

    def test_rejects_wrong_config_type(self) -> None:
        from primer.model.provider import AnthropicConfig

        provider = LLMProvider(
            id="o",
            provider=LLMProviderType.OPENRESPONSES,
            models=[LLMModel(name="gpt-4o-mini", context_length=1024)],
            config=AnthropicConfig(api_key=SecretStr("sk-x")),  # type: ignore[arg-type]
            limits=Limits(max_concurrency=1),
        )
        with pytest.raises(ConfigError, match="OpenResponsesConfig"):
            OpenResponsesLLM(provider)

    def test_openai_flavor_requires_key(self) -> None:
        with pytest.raises(ConfigError, match="api_key is required"):
            OpenResponsesLLM(_make_provider(flavor=OpenResponsesFlavor.OPENAI, api_key=""))

    def test_lmstudio_flavor_allows_empty_key(self) -> None:
        llm = OpenResponsesLLM(_make_provider(flavor=OpenResponsesFlavor.LMSTUDIO, api_key=""))
        assert llm._policy.require_api_key is False

    async def test_list_models(self) -> None:
        llm = OpenResponsesLLM(_make_provider(models=["a", "b"]))
        assert list(await llm.list_models()) == ["a", "b"]

    def test_get_client_lazy_and_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inst = _patched_client(monkeypatch)
        llm = OpenResponsesLLM(_make_provider())
        assert llm._client is None
        assert llm._get_client() is inst
        assert llm._get_client() is inst

    def test_get_client_empty_key_uses_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cls_mock = MagicMock(return_value=MagicMock())
        monkeypatch.setattr("primer.llm.openresponses.AsyncOpenAI", cls_mock)
        llm = OpenResponsesLLM(_make_provider(flavor=OpenResponsesFlavor.LMSTUDIO, api_key=""))
        llm._get_client()
        assert cls_mock.call_args.kwargs["api_key"] == "no-key-required"

    async def test_aclose_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inst = _patched_client(monkeypatch)
        inst.close = AsyncMock()
        llm = OpenResponsesLLM(_make_provider())
        llm._get_client()
        await llm.aclose()
        await llm.aclose()
        inst.close.assert_awaited_once()

    async def test_count_tokens_delegates(self) -> None:
        llm = OpenResponsesLLM(_make_provider())
        with patch(
            "primer.llm._tokenizer.openai.count_tokens_openai",
            return_value=42,
        ) as mock_count:
            n = await llm.count_tokens(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            )
        assert n == 42
        mock_count.assert_called_once()
