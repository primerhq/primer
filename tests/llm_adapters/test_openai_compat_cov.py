"""Coverage tests for the shared OpenAI-compatible helpers.

Lives outside ``tests/llm/`` (which the CI coverage sweep ignores) so
that ``primer.llm._openai_compat`` — the pure request-shaping and
SSE-translation base shared by ``OpenChatLLM`` and ``OpenRouterLLM`` —
is exercised by the *included* unit sweep.

Every helper here is a pure function: no network, no mocking. Chunks are
faked with ``SimpleNamespace`` so the ``getattr``-based translator sees
duck-typed openai SDK objects.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace as NS
from typing import Any

import pytest
from pydantic import BaseModel as PydanticBaseModel

from primer.llm._openai_compat import (
    _ToolCallInProgress,
    _StreamState,
    _build_sampling_params,
    _build_usage,
    _extract_extended_kwargs,
    _map_finish_reason,
    _messages_to_chat,
    _part_to_content,
    _response_format_to_param,
    _tool_choice_to_chat,
    _tool_to_chat,
    _translate_chunk,
)
from primer.model.chat import (
    AudioPart,
    DocumentPart,
    Done,
    ExtendedPart,
    ImagePart,
    Message,
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
from primer.model.except_ import ConfigError, UnsupportedContentError


# --------------------------------------------------------------------------- #
# _part_to_content                                                             #
# --------------------------------------------------------------------------- #


class TestPartToContent:
    def test_text_part(self) -> None:
        assert _part_to_content(TextPart(text="hi")) == {"type": "text", "text": "hi"}

    def test_image_url_plain(self) -> None:
        out = _part_to_content(ImagePart(url="https://example.com/x.png"))
        assert out == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/x.png"},
        }

    def test_image_url_with_detail(self) -> None:
        out = _part_to_content(ImagePart(url="https://example.com/x.png", detail="low"))
        assert out["image_url"] == {"url": "https://example.com/x.png", "detail": "low"}

    def test_image_data_becomes_data_uri(self) -> None:
        out = _part_to_content(ImagePart(data=b"\x89PNG", mime_type="image/png"))
        url = out["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG"

    def test_image_data_defaults_mime(self) -> None:
        out = _part_to_content(ImagePart(data=b"raw"))
        assert out["image_url"]["url"].startswith(
            "data:application/octet-stream;base64,"
        )

    def test_image_file_id_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="file_id"):
            _part_to_content(ImagePart(file_id="file-abc"))

    def test_document_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="document"):
            _part_to_content(DocumentPart(data=b"%PDF", mime_type="application/pdf"))

    def test_audio_extended_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="audio"):
            _part_to_content(
                ExtendedPart(extended=AudioPart(data=b"x", mime_type="audio/mp3"))
            )

    def test_video_extended_rejected(self) -> None:
        with pytest.raises(UnsupportedContentError, match="video"):
            _part_to_content(
                ExtendedPart(extended=VideoPart(url="https://example.com/v.mp4"))
            )


# --------------------------------------------------------------------------- #
# _messages_to_chat                                                            #
# --------------------------------------------------------------------------- #


class TestMessagesToChat:
    def test_user_text_only_string_content(self) -> None:
        assert _messages_to_chat(
            [Message(role="user", parts=[TextPart(text="hello")])]
        ) == [{"role": "user", "content": "hello"}]

    def test_system_message(self) -> None:
        assert _messages_to_chat(
            [Message(role="system", parts=[TextPart(text="be terse")])]
        ) == [{"role": "system", "content": "be terse"}]

    def test_user_image_becomes_content_array_with_leading_text(self) -> None:
        rows = _messages_to_chat(
            [
                Message(
                    role="user",
                    parts=[
                        TextPart(text="describe"),
                        ImagePart(url="https://example.com/cat.png"),
                    ],
                )
            ]
        )
        assert rows == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/cat.png"},
                    },
                ],
            }
        ]

    def test_image_only_content_array_no_text_prefix(self) -> None:
        rows = _messages_to_chat(
            [Message(role="user", parts=[ImagePart(url="https://x/y.png")])]
        )
        assert rows == [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
                ],
            }
        ]

    def test_empty_message_yields_none_content(self) -> None:
        rows = _messages_to_chat([Message.model_construct(role="assistant", parts=[])])
        assert rows == [{"role": "assistant", "content": None}]

    def test_assistant_tool_call_only_null_content(self) -> None:
        rows = _messages_to_chat(
            [
                Message(
                    role="assistant",
                    parts=[ToolCallPart(id="c1", name="lookup", arguments={"q": "abc"})],
                )
            ]
        )
        assert rows == [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"q": "abc"}'},
                    }
                ],
            }
        ]

    def test_assistant_text_plus_tool_call(self) -> None:
        rows = _messages_to_chat(
            [
                Message(
                    role="assistant",
                    parts=[
                        TextPart(text="checking"),
                        ToolCallPart(id="c1", name="search", arguments={}),
                    ],
                )
            ]
        )
        assert rows[0]["content"] == "checking"
        assert rows[0]["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_tool_role_one_row_per_result(self) -> None:
        rows = _messages_to_chat(
            [
                Message(
                    role="tool",
                    parts=[
                        ToolResultPart(id="c1", output="a"),
                        ToolResultPart(id="c2", output="b"),
                    ],
                )
            ]
        )
        assert rows == [
            {"role": "tool", "tool_call_id": "c1", "content": "a"},
            {"role": "tool", "tool_call_id": "c2", "content": "b"},
        ]

    def test_tool_role_with_non_result_raises(self) -> None:
        msg = Message.model_construct(role="tool", parts=[TextPart(text="nope")])
        with pytest.raises(UnsupportedContentError, match="ToolResultPart"):
            _messages_to_chat([msg])

    def test_tool_result_in_non_tool_message_raises(self) -> None:
        msg = Message.model_construct(
            role="assistant", parts=[ToolResultPart(id="c1", output="x")]
        )
        with pytest.raises(UnsupportedContentError, match="only valid inside a tool"):
            _messages_to_chat([msg])

    def test_full_conversation_roles_order(self) -> None:
        rows = _messages_to_chat(
            [
                Message(role="system", parts=[TextPart(text="sys")]),
                Message(role="user", parts=[TextPart(text="q")]),
                Message(
                    role="assistant",
                    parts=[ToolCallPart(id="c1", name="w", arguments={"c": "NYC"})],
                ),
                Message(role="tool", parts=[ToolResultPart(id="c1", output="sunny")]),
                Message(role="assistant", parts=[TextPart(text="done")]),
            ]
        )
        assert [r["role"] for r in rows] == [
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
        ]


# --------------------------------------------------------------------------- #
# tools / tool_choice / response_format                                        #
# --------------------------------------------------------------------------- #


class TestTools:
    def test_tool_to_chat_wraps_function_and_drops_toolset(self) -> None:
        tool = Tool(
            id="add",
            description="Add",
            toolset_id="math",
            args_schema={"type": "object", "properties": {}},
        )
        out = _tool_to_chat(tool)
        assert out["type"] == "function"
        assert out["function"]["name"] == "add"
        assert "toolset_id" not in out["function"]


class TestToolChoice:
    def test_none(self) -> None:
        assert _tool_choice_to_chat(None) is None

    @pytest.mark.parametrize("mode", ["auto", "required", "none"])
    def test_modes_pass_through(self, mode: str) -> None:
        assert _tool_choice_to_chat(mode) == mode

    def test_named_tool_wraps(self) -> None:
        assert _tool_choice_to_chat("add") == {
            "type": "function",
            "function": {"name": "add"},
        }


class TestResponseFormat:
    def test_none(self) -> None:
        assert _response_format_to_param(None) is None

    def test_dict_schema(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        assert _response_format_to_param(schema) == {
            "type": "json_schema",
            "json_schema": {"name": "schema", "schema": schema, "strict": True},
        }

    def test_pydantic_class(self) -> None:
        class Reply(PydanticBaseModel):
            value: int

        out = _response_format_to_param(Reply)
        assert out is not None
        assert out["json_schema"]["name"] == "Reply"
        assert out["json_schema"]["strict"] is True
        assert "value" in out["json_schema"]["schema"]["properties"]

    def test_invalid_type_raises(self) -> None:
        with pytest.raises(ConfigError, match="response_format"):
            _response_format_to_param(123)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# sampling / extended                                                          #
# --------------------------------------------------------------------------- #


class TestSampling:
    def test_all_none_empty(self) -> None:
        assert (
            _build_sampling_params(
                temperature=None, top_p=None, max_output_tokens=None, stop=None
            )
            == {}
        )

    def test_temp_top_p(self) -> None:
        out = _build_sampling_params(
            temperature=0.7, top_p=0.9, max_output_tokens=None, stop=None
        )
        assert out["temperature"] == 0.7
        assert out["top_p"] == 0.9

    def test_max_tokens_key(self) -> None:
        assert _build_sampling_params(
            temperature=None, top_p=None, max_output_tokens=512, stop=None
        ) == {"max_tokens": 512}

    def test_stop_native(self) -> None:
        assert _build_sampling_params(
            temperature=None, top_p=None, max_output_tokens=None, stop=["E", "\n"]
        ) == {"stop": ["E", "\n"]}


class TestExtendedKwargs:
    def test_none(self) -> None:
        assert _extract_extended_kwargs(None) == {}

    def test_empty(self) -> None:
        assert _extract_extended_kwargs({}) == {}

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
    def test_recognised_pass_through(self, key: str, value: Any) -> None:
        assert _extract_extended_kwargs({key: value}) == {key: value}

    def test_unknown_dropped(self) -> None:
        assert _extract_extended_kwargs({"frobnicate": True, "foobar": 1}) == {}

    def test_reasoning_keys_dropped(self) -> None:
        assert (
            _extract_extended_kwargs(
                {"reasoning_effort": "high", "reasoning_summary": "brief"}
            )
            == {}
        )

    def test_mixed_keeps_only_recognised(self) -> None:
        assert _extract_extended_kwargs({"seed": 1, "junk": 2}) == {"seed": 1}


# --------------------------------------------------------------------------- #
# finish-reason / usage helpers                                               #
# --------------------------------------------------------------------------- #


class TestFinishReason:
    @pytest.mark.parametrize(
        "raw, mapped",
        [
            ("stop", "stop"),
            ("length", "max_tokens"),
            ("tool_calls", "tool_use"),
            ("content_filter", "content_filter"),
            ("weird", "other"),
            (None, "other"),
        ],
    )
    def test_map(self, raw: str | None, mapped: str) -> None:
        assert _map_finish_reason(raw) == mapped


class TestBuildUsage:
    def test_none(self) -> None:
        assert _build_usage(None) is None

    def test_partial_none(self) -> None:
        assert _build_usage(NS(prompt_tokens=None, completion_tokens=4)) is None
        assert _build_usage(NS(prompt_tokens=4, completion_tokens=None)) is None

    def test_full(self) -> None:
        out = _build_usage(NS(prompt_tokens=11, completion_tokens=7))
        assert isinstance(out, Usage)
        assert (out.input_tokens, out.output_tokens, out.cumulative) == (11, 7, False)


# --------------------------------------------------------------------------- #
# dataclass state                                                             #
# --------------------------------------------------------------------------- #


class TestState:
    def test_stream_state_defaults(self) -> None:
        state = _StreamState()
        assert state.stream_started is False
        assert state.saw_function_call is False
        assert state.tool_calls == {}
        assert state.request_id is None
        assert state.model == ""

    def test_tool_call_in_progress_defaults(self) -> None:
        tc = _ToolCallInProgress(call_id="c1", name="search")
        assert tc.arguments_buffer == ""
        assert tc.index == 0


# --------------------------------------------------------------------------- #
# _translate_chunk                                                            #
# --------------------------------------------------------------------------- #


def _delta_chunk(
    *, role=None, content=None, tool_calls=None, finish_reason=None, usage=None
) -> NS:
    return NS(
        id="chatcmpl-1",
        model="gpt-test",
        choices=[
            NS(
                index=0,
                delta=NS(role=role, content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        usage=usage,
    )


class TestTranslateChunk:
    def test_role_chunk_emits_stream_start(self) -> None:
        state = _StreamState()
        out = _translate_chunk(_delta_chunk(role="assistant"), state)
        assert len(out) == 1 and isinstance(out[0], StreamStart)
        assert out[0].request_id == "chatcmpl-1"
        assert out[0].model == "gpt-test"
        assert state.stream_started is True

    def test_role_and_content_same_chunk(self) -> None:
        state = _StreamState()
        out = _translate_chunk(_delta_chunk(role="assistant", content="hi"), state)
        assert [type(e).__name__ for e in out] == ["StreamStart", "TextDelta"]

    def test_text_delta_after_start(self) -> None:
        state = _StreamState()
        _translate_chunk(_delta_chunk(role="assistant"), state)
        out = _translate_chunk(_delta_chunk(content="hello"), state)
        assert isinstance(out[0], TextDelta) and out[0].text == "hello"

    def test_tool_call_start_then_delta_append(self) -> None:
        state = _StreamState()
        _translate_chunk(_delta_chunk(role="assistant"), state)
        start = _translate_chunk(
            _delta_chunk(
                tool_calls=[
                    NS(index=0, id="call_z", function=NS(name="search", arguments=""))
                ]
            ),
            state,
        )
        assert isinstance(start[0], ToolCallStart)
        assert start[0].id == "call_z" and start[0].name == "search"
        assert state.saw_function_call is True
        # append onto existing tracked call
        more = _translate_chunk(
            _delta_chunk(
                tool_calls=[
                    NS(index=0, id=None, function=NS(name=None, arguments='{"q":1}'))
                ]
            ),
            state,
        )
        assert isinstance(more[0], ToolCallDelta)
        assert more[0].arguments_delta == '{"q":1}' and more[0].id == "call_z"

    def test_tool_call_start_with_initial_args_emits_start_and_delta(self) -> None:
        state = _StreamState()
        _translate_chunk(_delta_chunk(role="assistant"), state)
        out = _translate_chunk(
            _delta_chunk(
                tool_calls=[
                    NS(index=0, id="call_a", function=NS(name="f", arguments='{"a":'))
                ]
            ),
            state,
        )
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["ToolCallStart", "ToolCallDelta"]
        assert out[1].arguments_delta == '{"a":'

    def test_finish_tool_calls_flushes_end_then_done(self) -> None:
        state = _StreamState()
        _translate_chunk(_delta_chunk(role="assistant"), state)
        _translate_chunk(
            _delta_chunk(
                tool_calls=[
                    NS(
                        index=0,
                        id="call_q",
                        function=NS(name="lookup", arguments='{"q":"v"}'),
                    )
                ]
            ),
            state,
        )
        out = _translate_chunk(_delta_chunk(finish_reason="tool_calls"), state)
        assert [type(e).__name__ for e in out] == ["ToolCallEnd", "Done"]
        end = out[0]
        assert isinstance(end, ToolCallEnd)
        assert end.id == "call_q" and end.arguments == {"q": "v"}
        assert out[1].stop_reason == "tool_use" and out[1].raw_reason == "tool_calls"

    def test_finish_tool_calls_invalid_json_defaults_empty(self) -> None:
        state = _StreamState()
        _translate_chunk(_delta_chunk(role="assistant"), state)
        _translate_chunk(
            _delta_chunk(
                tool_calls=[
                    NS(index=0, id="c", function=NS(name="f", arguments="{not-json"))
                ]
            ),
            state,
        )
        out = _translate_chunk(_delta_chunk(finish_reason="tool_calls"), state)
        end = out[0]
        assert isinstance(end, ToolCallEnd) and end.arguments == {}

    def test_finish_stop_with_usage(self) -> None:
        state = _StreamState()
        _translate_chunk(_delta_chunk(role="assistant", content="ok"), state)
        out = _translate_chunk(
            _delta_chunk(finish_reason="stop", usage=NS(prompt_tokens=12, completion_tokens=6)),
            state,
        )
        assert [type(e).__name__ for e in out] == ["Usage", "Done"]
        assert out[0].input_tokens == 12 and out[0].output_tokens == 6
        assert out[1].stop_reason == "stop"

    def test_finish_length_maps_max_tokens(self) -> None:
        state = _StreamState()
        state.stream_started = True
        out = _translate_chunk(_delta_chunk(finish_reason="length"), state)
        assert isinstance(out[-1], Done) and out[-1].stop_reason == "max_tokens"

    def test_finish_content_filter(self) -> None:
        state = _StreamState()
        state.stream_started = True
        out = _translate_chunk(_delta_chunk(finish_reason="content_filter"), state)
        assert isinstance(out[-1], Done) and out[-1].stop_reason == "content_filter"

    def test_trailing_usage_only_no_choices(self) -> None:
        state = _StreamState()
        state.stream_started = True
        chunk = NS(id="x", model="m", choices=[], usage=NS(prompt_tokens=3, completion_tokens=4))
        out = _translate_chunk(chunk, state)
        assert len(out) == 1 and isinstance(out[0], Usage)
        assert out[0].input_tokens == 3

    def test_no_choices_no_usage_empty(self) -> None:
        state = _StreamState()
        state.stream_started = True
        chunk = NS(id="x", model="m", choices=[], usage=None)
        assert _translate_chunk(chunk, state) == []

    def test_delta_none_produces_nothing(self) -> None:
        state = _StreamState()
        chunk = NS(
            id="x",
            model="m",
            choices=[NS(index=0, delta=None, finish_reason=None)],
            usage=None,
        )
        assert _translate_chunk(chunk, state) == []
