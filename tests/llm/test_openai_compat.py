"""Direct unit tests for the OpenAI-compat helpers.

These tests give ``primer.llm._openai_compat`` direct coverage of the
request-shaping and SSE-translation helpers shared by ``OpenChatLLM``
and (soon) ``OpenRouterLLM``. They mirror representative cases from
``tests/llm/test_openchat.py`` so future refactors of either adapter
don't have to triangulate through ``OpenChatLLM`` to verify the helper
contract.
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest
from pydantic import BaseModel as PydanticBaseModel

from primer.llm._openai_compat import (
    _build_sampling_params,
    _build_usage,
    _extract_extended_kwargs,
    _map_finish_reason,
    _messages_to_chat,
    _response_format_to_param,
    _StreamState,
    _tool_choice_to_chat,
    _tool_to_chat,
    _translate_chunk,
)
from primer.model.chat import (
    Done,
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
)
from primer.model.except_ import ConfigError, UnsupportedContentError


class TestMessagesToChat:
    def test_simple_user_text_uses_string_content(self) -> None:
        rows = _messages_to_chat(
            [Message(role="user", parts=[TextPart(text="hello")])]
        )
        assert rows == [{"role": "user", "content": "hello"}]

    def test_user_with_image_uses_content_array(self) -> None:
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

    def test_assistant_tool_calls_only_emits_null_content(self) -> None:
        rows = _messages_to_chat(
            [
                Message(
                    role="assistant",
                    parts=[
                        ToolCallPart(
                            id="call_x", name="lookup", arguments={"q": "abc"}
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
                        "id": "call_x",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"q": "abc"}',
                        },
                    }
                ],
            }
        ]

    def test_tool_role_emits_one_row_per_result(self) -> None:
        rows = _messages_to_chat(
            [
                Message(
                    role="tool",
                    parts=[
                        ToolResultPart(id="call_x", output="result_a"),
                        ToolResultPart(id="call_y", output="result_b"),
                    ],
                )
            ]
        )
        assert rows == [
            {"role": "tool", "tool_call_id": "call_x", "content": "result_a"},
            {"role": "tool", "tool_call_id": "call_y", "content": "result_b"},
        ]

    def test_tool_role_with_non_tool_result_raises(self) -> None:
        msg = Message.model_construct(role="tool", parts=[TextPart(text="nope")])
        with pytest.raises(UnsupportedContentError, match="ToolResultPart"):
            _messages_to_chat([msg])


class TestTools:
    def test_tool_to_chat_wraps_function_envelope(self) -> None:
        tool = Tool(
            id="add",
            description="Add two numbers",
            toolset_id="math_kit",
            args_schema={
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
            },
        )
        out = _tool_to_chat(tool)
        assert out == {
            "type": "function",
            "function": {
                "name": "add",
                "description": "Add two numbers",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "number"},
                        "b": {"type": "number"},
                    },
                    "required": ["a", "b"],
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
        assert _tool_choice_to_chat("add") == {
            "type": "function",
            "function": {"name": "add"},
        }


class TestResponseFormat:
    def test_none_returns_none(self) -> None:
        assert _response_format_to_param(None) is None

    def test_dict_schema_root_level_json_schema(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        out = _response_format_to_param(schema)
        assert out == {
            "type": "json_schema",
            "json_schema": {
                "name": "schema",
                "schema": schema,
                "strict": True,
            },
        }

    def test_pydantic_class_uses_class_name_and_schema(self) -> None:
        class Reply(PydanticBaseModel):
            value: int

        out = _response_format_to_param(Reply)
        assert out is not None
        assert out["type"] == "json_schema"
        assert out["json_schema"]["name"] == "Reply"
        assert "value" in out["json_schema"]["schema"]["properties"]
        assert out["json_schema"]["strict"] is True

    def test_invalid_type_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="response_format"):
            _response_format_to_param(123)  # type: ignore[arg-type]


class TestStopReason:
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
    def test_map_finish_reason(self, raw: str | None, mapped: str) -> None:
        assert _map_finish_reason(raw) == mapped


class TestBuildUsage:
    def test_none_returns_none(self) -> None:
        assert _build_usage(None) is None

    def test_partial_token_counts_returns_none(self) -> None:
        assert _build_usage(NS(prompt_tokens=None, completion_tokens=4)) is None
        assert _build_usage(NS(prompt_tokens=4, completion_tokens=None)) is None

    def test_full_counts_returns_usage_event(self) -> None:
        out = _build_usage(NS(prompt_tokens=11, completion_tokens=7))
        assert isinstance(out, Usage)
        assert out.input_tokens == 11
        assert out.output_tokens == 7
        assert out.cumulative is False


class TestStreamMapping:
    def test_first_chunk_with_role_emits_stream_start(self) -> None:
        state = _StreamState()
        chunk = NS(
            id="chatcmpl-A",
            model="gpt-test",
            choices=[
                NS(
                    index=0,
                    delta=NS(role="assistant", content=None, tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        out = _translate_chunk(chunk, state)
        assert len(out) == 1
        assert isinstance(out[0], StreamStart)
        assert out[0].request_id == "chatcmpl-A"
        assert out[0].model == "gpt-test"
        assert state.stream_started is True

    def test_text_delta_after_start(self) -> None:
        state = _StreamState()
        _translate_chunk(
            NS(
                id="x",
                model="m",
                choices=[
                    NS(
                        index=0,
                        delta=NS(role="assistant", content=None, tool_calls=None),
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
                    delta=NS(role=None, content="hello", tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        out = _translate_chunk(chunk, state)
        assert len(out) == 1
        assert isinstance(out[0], TextDelta)
        assert out[0].text == "hello"

    def test_tool_call_start_then_delta(self) -> None:
        state = _StreamState()
        _translate_chunk(
            NS(
                id="x",
                model="m",
                choices=[
                    NS(
                        index=0,
                        delta=NS(role="assistant", content=None, tool_calls=None),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            state,
        )
        first = _translate_chunk(
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
                                    id="call_z",
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
        assert len(first) == 1
        assert isinstance(first[0], ToolCallStart)
        assert first[0].id == "call_z"
        assert first[0].name == "search"
        assert state.saw_function_call is True

        second = _translate_chunk(
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
                                    id=None,
                                    type=None,
                                    function=NS(name=None, arguments='{"q":1}'),
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
        assert len(second) == 1
        assert isinstance(second[0], ToolCallDelta)
        assert second[0].arguments_delta == '{"q":1}'
        assert second[0].id == "call_z"

    def test_finish_reason_tool_calls_flushes_end_then_done(self) -> None:
        state = _StreamState()
        _translate_chunk(
            NS(
                id="x",
                model="m",
                choices=[
                    NS(
                        index=0,
                        delta=NS(role="assistant", content=None, tool_calls=None),
                        finish_reason=None,
                    )
                ],
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
                                    id="call_q",
                                    type="function",
                                    function=NS(
                                        name="lookup",
                                        arguments='{"q":"value"}',
                                    ),
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
        assert end.id == "call_q"
        assert end.arguments == {"q": "value"}
        done = out[1]
        assert isinstance(done, Done)
        assert done.stop_reason == "tool_use"
        assert done.raw_reason == "tool_calls"

    def test_final_chunk_with_usage_emits_usage_then_done(self) -> None:
        state = _StreamState()
        _translate_chunk(
            NS(
                id="x",
                model="m",
                choices=[
                    NS(
                        index=0,
                        delta=NS(role="assistant", content="ok", tool_calls=None),
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
                    finish_reason="stop",
                )
            ],
            usage=NS(prompt_tokens=12, completion_tokens=6),
        )
        out = _translate_chunk(chunk, state)
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["Usage", "Done"]
        usage = out[0]
        assert isinstance(usage, Usage)
        assert usage.input_tokens == 12
        assert usage.output_tokens == 6

    def test_trailing_usage_only_chunk_no_choices(self) -> None:
        state = _StreamState()
        state.stream_started = True
        chunk = NS(
            id="x",
            model="m",
            choices=[],
            usage=NS(prompt_tokens=3, completion_tokens=4),
        )
        out = _translate_chunk(chunk, state)
        assert len(out) == 1
        assert isinstance(out[0], Usage)
        assert out[0].input_tokens == 3
        assert out[0].output_tokens == 4


class TestStreamStateDefaults:
    def test_fresh_state_has_no_stream_started_and_empty_calls(self) -> None:
        state = _StreamState()
        assert state.stream_started is False
        assert state.saw_function_call is False
        assert state.tool_calls == {}
        assert state.request_id is None
        assert state.model == ""


class TestBuildSamplingParams:
    def test_all_none_returns_empty(self) -> None:
        out = _build_sampling_params(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=None,
        )
        assert out == {}

    def test_temperature_and_top_p_pass_through(self) -> None:
        out = _build_sampling_params(
            temperature=0.7,
            top_p=0.9,
            max_output_tokens=None,
            stop=None,
        )
        assert out["temperature"] == 0.7
        assert out["top_p"] == 0.9

    def test_max_output_tokens_maps_to_max_tokens(self) -> None:
        out = _build_sampling_params(
            temperature=None,
            top_p=None,
            max_output_tokens=512,
            stop=None,
        )
        assert out == {"max_tokens": 512}

    def test_stop_passes_through_natively(self) -> None:
        out = _build_sampling_params(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=["END", "\n\n"],
        )
        assert out == {"stop": ["END", "\n\n"]}


class TestExtractExtendedKwargs:
    def test_none_returns_empty(self) -> None:
        assert _extract_extended_kwargs(None) == {}

    def test_empty_dict_returns_empty(self) -> None:
        assert _extract_extended_kwargs({}) == {}

    @pytest.mark.parametrize(
        "key, value",
        [
            ("parallel_tool_calls", False),
            ("presence_penalty", 0.5),
            ("seed", 7),
            ("user", "u-123"),
        ],
    )
    def test_recognised_keys_pass_through(self, key: str, value: Any) -> None:
        assert _extract_extended_kwargs({key: value}) == {key: value}

    def test_unknown_keys_dropped(self) -> None:
        assert _extract_extended_kwargs({"frobnicate": True, "foobar": 1}) == {}

    def test_reasoning_keys_dropped_on_chat_completions(self) -> None:
        # Chat Completions has no reasoning channel; both reasoning_*
        # keys are unknown and dropped.
        assert _extract_extended_kwargs(
            {"reasoning_effort": "high", "reasoning_summary": "concise"}
        ) == {}
