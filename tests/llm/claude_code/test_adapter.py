"""ClaudeCodeLLM: drive the operator's claude CLI as an LLM provider (the claude_code plan, task 7).

One fresh CLI process per model step. The stream tests run against the
fake_claude double (task 2), with the FAKE_CLAUDE_* names passed through the
row's pass_env -- which also proves pass_env works, since the record pins the
child's whole environment.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from primer.common.context_overflow import is_context_overflow, is_context_overflow_error
from primer.llm._reasoning import claude_code_extended
from primer.llm._retry import is_retryable
from primer.llm.claude_code import ClaudeCodeLLM
from primer.llm.claude_code.history import replay_tool_id
from primer.llm.claude_code.process import ALLOWED
from primer.llm.claude_code.wire import build_argv, tail_lines
from primer.model.chat import (
    Done,
    Error as ChatError,
    Message,
    TextDelta,
    TextPart,
    Tool,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    ConfigError,
    ProviderError,
    ProviderTimeoutError,
    TokenCounterUnavailable,
    TransientError,
)
from primer.model.model_profile import ModelProfileConfig, ReasoningLevel
from primer.model.provider import ClaudeCodeConfig, Limits, LLMProvider, LLMProviderType

from tests.llm.claude_code.fake_claude import fake_claude  # noqa: F401  (fixture)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude_code"

PASS_ENV = ["FAKE_CLAUDE_FIXTURE", "FAKE_CLAUDE_RECORD", "FAKE_CLAUDE_TOOLS_LIST", "FAKE_CLAUDE_DELAY"]


def _provider(binary: str, *, max_concurrency: int = 2, request_timeout: float | None = None,
              connect_timeout: float | None = None) -> LLMProvider:
    limits = {"max_concurrency": max_concurrency}
    if request_timeout is not None:
        limits["request_timeout_seconds"] = request_timeout
    if connect_timeout is not None:
        limits["connect_timeout_seconds"] = connect_timeout
    return LLMProvider(
        id="cc-test",
        provider=LLMProviderType.CLAUDE_CODE,
        config=ClaudeCodeConfig(binary_path=binary, pass_env=PASS_ENV),
        limits=Limits(**limits),
    )


def _user(text: str) -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def _weather_tool() -> Tool:
    return Tool(
        id="weather__get_weather",
        description="Get the weather for a city.",
        toolset_id="weather",
        args_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    )


def _stream_env(monkeypatch, tmp_path: Path, fixture: str, *, tools_list: bool = False, delay: float | None = None) -> Path:
    """Point the double at one fixture and the record file; FAKE_* rides in pass_env."""
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(FIXTURES / f"{fixture}.ndjson"))
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    if tools_list:
        monkeypatch.setenv("FAKE_CLAUDE_TOOLS_LIST", "1")
    if delay is not None:
        monkeypatch.setenv("FAKE_CLAUDE_DELAY", str(delay))
    return record


def _fs_tool() -> Tool:
    return Tool(
        id="fs__read",
        description="Read a file.",
        toolset_id="fs",
        args_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )


def test_tail_lines_tool_round():
    """A pending tool round replays as one assistant line then one user line."""
    tail = [
        Message(role="assistant", parts=[
            ToolCallPart(id="call_1", name="fs__read", arguments={"path": "/etc/hosts"}),
        ]),
        Message(role="tool", parts=[
            ToolResultPart(id="call_1", output="127.0.0.1 localhost"),
        ]),
    ]
    a, u = tail_lines(tail)
    assert a["type"] == "assistant" and u["type"] == "user"
    assert a["message"]["content"][0]["type"] == "tool_use"
    assert a["message"]["content"][0]["name"] == "mcp__p__fs__read"
    assert a["message"]["content"][0]["input"] == {"path": "/etc/hosts"}
    assert u["message"]["content"][0]["type"] == "tool_result"
    assert u["message"]["content"][0]["tool_use_id"] == a["message"]["content"][0]["id"] == replay_tool_id("call_1")
    assert u["message"]["content"][0]["content"] == "127.0.0.1 localhost"


def test_tail_lines_user_tail():
    """A user tail becomes one user line with its text (and images) as blocks."""
    tail = [Message(role="user", parts=[TextPart(text="what is 21?")])]
    (line,) = tail_lines(tail)
    assert line == {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "what is 21?"}]}}


def test_build_argv_order():
    assert build_argv("claude", Path("/t"), model="sonnet", mcp_url=None, effort=None, json_schema=None) == [
        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose", "--include-partial-messages", "--tools", "", "--strict-mcp-config",
        "--setting-sources", "", "--no-session-persistence", "--disable-slash-commands",
        "--system-prompt-file", "/t/system.md", "--model", "sonnet",
    ]
    # the optional flags append, in order: the bridge, then the effort, then the schema
    argv = build_argv(
        "claude", Path("/t"), model="opus",
        mcp_url="http://127.0.0.1:1234/tok/mcp", effort="high",
        json_schema={"type": "object"},
    )
    i = argv.index("--mcp-config")
    assert json.loads(argv[i + 1]) == {"mcpServers": {"p": {"type": "http", "url": "http://127.0.0.1:1234/tok/mcp"}}}
    assert argv[i + 2 : i + 4] == ["--allowedTools", "mcp__p"]
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--thinking-display") + 1] == "summarized"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}


async def test_tool_round_end_to_end(fake_claude, tmp_path, monkeypatch):
    """A tool round streams to message_stop/tool_use; argv, env and the bridge pin the wire."""
    record = _stream_env(monkeypatch, tmp_path, "parallel_tools", tools_list=True)
    llm = ClaudeCodeLLM(_provider(fake_claude))
    events = [
        e async for e in llm.stream(
            model="sonnet",
            messages=[
                Message(role="system", parts=[TextPart(text="answer concisely")]),
                _user("the weather in Paris and Tokyo"),
            ],
            tools=[_weather_tool()],
        )
    ]
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "tool_use"
    rec = json.loads(record.read_text(encoding="utf-8"))
    assert rec["argv"][1:4] == ["-p", "--input-format", "stream-json"]
    assert rec["argv"][rec["argv"].index("--tools") + 1] == ""
    assert rec["argv"][rec["argv"].index("--allowedTools") + 1] == "mcp__p"
    assert [t["name"] for t in rec["tools_list"]] == ["weather__get_weather"]
    allowed = ALLOWED | {
        "FAKE_CLAUDE_FIXTURE", "FAKE_CLAUDE_RECORD", "FAKE_CLAUDE_TOOLS_LIST",
        "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT",
    }
    assert set(rec["env"]) <= allowed


async def test_system_prompt_goes_through_a_file(fake_claude, tmp_path, monkeypatch):
    """A 2 MB history reaches the model through system.md, never through argv."""
    record = _stream_env(monkeypatch, tmp_path, "text_with_tool")
    llm = ClaudeCodeLLM(_provider(fake_claude))
    big = "x" * (256 * 1024)
    messages = [
        Message(role="system", parts=[TextPart(text="be brief")]),
        *[Message(role="user", parts=[TextPart(text=big)]) for _ in range(4)],
        _user("summarise"),
    ]
    events = [e async for e in llm.stream(model="sonnet", messages=messages)]
    assert events[-1].stop_reason == "stop"
    rec = json.loads(record.read_text(encoding="utf-8"))
    assert "--system-prompt-file" in rec["argv"] and "--system-prompt" not in rec["argv"]
    # the whole argv is small while the system prompt is 2 MB: the prompt went to the file
    assert len(" ".join(rec["argv"])) < 10_000


async def test_tail_is_sent_on_stdin(fake_claude, tmp_path, monkeypatch):
    """A tool-round tail rides on stdin as one assistant line then one tool_result line."""
    record = _stream_env(monkeypatch, tmp_path, "text_with_tool")
    llm = ClaudeCodeLLM(_provider(fake_claude))
    messages = [
        Message(role="assistant", parts=[
            ToolCallPart(id="call_1", name="fs__read", arguments={"path": "/etc/hosts"}),
        ]),
        Message(role="tool", parts=[
            ToolResultPart(id="call_1", output="127.0.0.1 localhost"),
        ]),
    ]
    events = [e async for e in llm.stream(model="sonnet", messages=messages, tools=[_fs_tool()])]
    assert events[-1].stop_reason == "stop"
    stdin = json.loads(record.read_text(encoding="utf-8"))["stdin"]
    lines = [json.loads(line) for line in stdin.splitlines() if line.strip()]
    assert [line["type"] for line in lines] == ["assistant", "user"]
    a, u = lines
    assert a["message"]["content"][0]["name"] == "mcp__p__fs__read"
    assert u["message"]["content"][0]["tool_use_id"] == a["message"]["content"][0]["id"] == replay_tool_id("call_1")


async def test_stop_mid_stream_cleans_up(fake_claude, tmp_path, monkeypatch):
    """Stopping mid-stream leaves no temp dir, no live child and a free rate-limit slot;
    the bridge step is deregistered with the kill."""
    _stream_env(monkeypatch, tmp_path, "thinking", delay=0.2)
    from primer.coordinator.in_memory import InMemoryRateLimiter
    from primer.llm.claude_code.bridge import get_bridge

    limiter = InMemoryRateLimiter()
    bridge = await get_bridge()
    tokens_before = set(bridge._steps)
    llm = ClaudeCodeLLM(_provider(fake_claude, max_concurrency=1), rate_limiter=limiter)
    # WITH tools, so this stream registers a bridge step the kill must deregister.
    it = llm.stream(model="sonnet", messages=[_user("count to 21")], tools=[_fs_tool()])
    await anext(it)
    # Right after the first event the step IS registered: the stream ran WITH
    # tools, so a new bridge token exists (the kill must deregister it).
    assert set(bridge._steps) - tokens_before, "the step was registered before the first event"
    await it.aclose()
    await asyncio.sleep(0.5)  # margin over the shielded cleanup
    root = Path(tempfile.gettempdir())
    leftovers = [p for p in root.iterdir() if p.name.startswith(f"primer-cc-{os.getpid()}-")]
    assert leftovers == []
    # the SPECIFIC child pid is gone (the fake recorded it); a host-wide pgrep of
    # the double's name is flaky under xdist and parallel worktrees.
    rec = json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))
    try:
        os.kill(rec["pid"], 0)
    except ProcessLookupError:
        pass
    else:
        pytest.fail(f"the fake CLI (pid {rec['pid']}) is still alive after the kill")
    # the bridge step this stream registered is unregistered (its token is gone).
    assert set(bridge._steps) <= tokens_before
    # the llm:{id} slot was released with the stream (it would block otherwise)
    async with asyncio.timeout(2):
        async with await limiter.acquire("llm:cc-test", max_concurrency=1):
            pass


async def test_a_stdout_line_longer_than_64k_streams_cleanly(fake_claude, tmp_path, monkeypatch):
    """A line beyond the 64 KiB readline limit (child_process raises the limit; the
    300 KB size also straddles the 256 KB single-read chunk, so it must): a long
    answer must not ValueError the step into an Error AFTER Done."""
    big = "x" * (300 * 1024)
    fixture = tmp_path / "big_line.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": big}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"}),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(fixture))
    llm = ClaudeCodeLLM(_provider(fake_claude))
    events = [e async for e in llm.stream(model="sonnet", messages=[_user("hi")])]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert len(text) == 300 * 1024
    assert not any(isinstance(e, (ChatError,)) for e in events)
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "stop"


async def test_structured_output_comes_from_the_result_line_as_one_text_delta(fake_claude, tmp_path, monkeypatch):
    """Spec 6.7 v1.4: the validated answer is the result line's structured_output,
    emitted as ONE TextDelta followed by Done(stop); the attempt's own input is
    never streamed (it is unvalidated and may have been retried)."""
    _stream_env(monkeypatch, tmp_path, "structured_output")
    llm = ClaudeCodeLLM(_provider(fake_claude))
    events = [
        e async for e in llm.stream(
            model="sonnet", messages=[_user("answer in json")],
            response_format={"type": "object", "properties": {"prime": {"type": "boolean"}}},
        )
    ]
    assert not any(isinstance(e, (ToolCallStart, ToolCallDelta, ToolCallEnd)) for e in events)
    text = [e for e in events if isinstance(e, TextDelta)]
    assert len(text) == 1
    assert json.loads(text[0].text) == {"prime": True}
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "stop"


async def test_structured_output_retry_emits_only_the_result_value(fake_claude, tmp_path, monkeypatch):
    """Spec 6.7 v1.4: the first attempt's input is invalid, the CLI makes the model
    retry; only the result line's structured_value is emitted -- never the first
    attempt's input, and the held Done is emitted once, at the result."""
    _stream_env(monkeypatch, tmp_path, "structured_output_retry")
    llm = ClaudeCodeLLM(_provider(fake_claude))
    events = [
        e async for e in llm.stream(
            model="sonnet", messages=[_user("answer in json")],
            response_format={"type": "object", "properties": {"prime": {"type": "boolean"}}},
        )
    ]
    assert not any(isinstance(e, (ToolCallStart, ToolCallDelta, ToolCallEnd)) for e in events)
    text = [e for e in events if isinstance(e, TextDelta)]
    assert len(text) == 1, "exactly one answer, at the result line"
    assert json.loads(text[0].text) == {"prime": True}
    assert '"yes"' not in text[0].text, "the first (invalid) attempt's input is never emitted"
    dones = [e for e in events if isinstance(e, Done)]
    assert len(dones) == 1 and dones[0].stop_reason == "stop"


async def test_structured_output_failure_never_emits_an_answer(fake_claude, tmp_path, monkeypatch):
    """Spec 6.7 v1.4: a failing result (retries exhausted) is an error, not an
    answer: the held Done is dropped, nothing is emitted after the fatal Error."""
    fixture = tmp_path / "structured_failed.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s-fail", "tools": ["StructuredOutput"]}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "struct_1", "name": "StructuredOutput", "input": {}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"prime": true}'}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            json.dumps({"type": "result", "subtype": "error_max_structured_output_retries", "is_error": True, "result": "Structured output retries exhausted", "structured_output": None, "session_id": "s-fail"}),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(fixture))
    llm = ClaudeCodeLLM(_provider(fake_claude))
    events = [
        e async for e in llm.stream(
            model="sonnet", messages=[_user("answer in json")],
            response_format={"type": "object", "properties": {"prime": {"type": "boolean"}}},
        )
    ]
    # Events (Usage) were sent, so the failure is a terminal fatal Error:
    # no answer text, no Done, nothing after the Error.
    assert not any(isinstance(e, TextDelta) for e in events)
    assert not any(isinstance(e, Done) for e in events)
    errors = [e for e in events if isinstance(e, ChatError)]
    assert len(errors) == 1 and errors[0].fatal and errors[0].message
    assert events[-1] is errors[0]


async def test_pre_event_failing_result_is_scrubbed_and_raised(fake_claude, tmp_path, monkeypatch):
    """Spec 6.6/6.7: a failing structured result that arrives BEFORE any event is
    raised (never yielded) and its message is scrubbed through describe_failure."""
    secret = "sk-struct-fail-abcdef0123456789"
    fixture = tmp_path / "structured_fail_pre.ndjson"
    fixture.write_text(
        json.dumps({"type": "result", "subtype": "error_max_structured_output_retries", "is_error": True,
                    "result": f"Structured output retries exceeded (trace: Authorization: Bearer {secret})",
                    "session_id": "s-fail"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(fixture))
    llm = ClaudeCodeLLM(_provider(fake_claude))
    with pytest.raises(ProviderError) as exc:
        async for _ in llm.stream(
            model="sonnet", messages=[_user("answer in json")],
            response_format={"type": "object", "properties": {"prime": {"type": "boolean"}}},
        ):
            pass
    assert exc.value.code == "claude_code_result_error"
    assert "Structured output retries exceeded" in exc.value.message
    assert secret not in exc.value.message
    assert "[REDACTED]" in exc.value.message


async def test_pre_event_overflow_raises_a_bad_request_with_the_cli_text(fake_claude, tmp_path, monkeypatch):
    """Spec 6.6 v1.6: a result the CLI rejects as too long, BEFORE any event,
    is a BadRequestError that keeps the CLI text (through describe_failure,
    which keeps it for this class and caps its length), so is_context_overflow
    (and the reactive compaction) recognise it."""
    fixture = tmp_path / "overflow_pre.ndjson"
    fixture.write_text(
        json.dumps({"type": "result", "subtype": "error", "is_error": True,
                    "result": "Prompt is too long: 210000 tokens > 200000 maximum",
                    "session_id": "s-ov-pre"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(fixture))
    llm = ClaudeCodeLLM(_provider(fake_claude))
    with pytest.raises(BadRequestError) as exc:
        async for _ in llm.stream(model="sonnet", messages=[_user("hi")]):
            pass
    assert exc.value.code == "bad_request"
    assert "Prompt is too long" in exc.value.message
    assert is_context_overflow(exc.value)
    # A long CLI message keeps its head (the phrase) and is capped, not dropped.
    long_text = "Prompt is too long: " + "x" * 5000
    fixture.write_text(
        json.dumps({"type": "result", "subtype": "error", "is_error": True,
                    "result": long_text, "session_id": "s-ov-pre"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(BadRequestError) as exc:
        async for _ in llm.stream(model="sonnet", messages=[_user("hi")]):
            pass
    assert is_context_overflow(exc.value)
    assert "Prompt is too long" in exc.value.message
    assert len(exc.value.message) <= 4010


async def test_mid_stream_overflow_yields_a_fatal_bad_request(fake_claude, tmp_path, monkeypatch):
    """Spec 6.6 v1.5: the same too-long result AFTER the first event is a fatal
    yielded Error with code bad_request (never raised), and
    is_context_overflow_error recognises it for the agent loop."""
    fixture = tmp_path / "overflow_mid.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s-ov-mid"}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok so far"}}}),
            json.dumps({"type": "result", "subtype": "error", "is_error": True,
                        "result": "Prompt is too long: 210000 tokens > 200000 maximum",
                        "session_id": "s-ov-mid"}),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(fixture))
    llm = ClaudeCodeLLM(_provider(fake_claude))
    events = [e async for e in llm.stream(model="sonnet", messages=[_user("hi")])]
    assert isinstance(events[-1], ChatError)
    assert events[-1].fatal and events[-1].code == "bad_request"
    assert "Prompt is too long" in events[-1].message
    assert is_context_overflow_error(events[-1])


async def test_disabled_command_reply_is_a_normal_answer(fake_claude, tmp_path, monkeypatch):
    """Spec 6.7 v1.6: a user message that is a known command name gets the
    CLI's "isn't available" reply, VERIFIED on the real CLI 2.1.322: the
    synthetic assistant line is NOT translated (that would double the text),
    and the successful result line that arrives before any Done answers with
    its text as one TextDelta, then Done(stop)."""
    _stream_env(monkeypatch, tmp_path, "local_command_reply")
    llm = ClaudeCodeLLM(_provider(fake_claude))
    events = [e async for e in llm.stream(model="sonnet", messages=[_user("/context")])]
    text = [e for e in events if isinstance(e, TextDelta)]
    assert len(text) == 1
    assert text[0].text == "/context isn't available in this environment."
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "stop"
    assert not any(isinstance(e, ChatError) for e in events)


async def test_result_without_value_and_no_text_yields_no_answer(fake_claude, tmp_path, monkeypatch):
    """Spec 6.7 v1.5: a successful result line WITHOUT structured_output and with
    no buffered text yields no answer (the held Done is emitted, nothing before
    it) -- it is not an error, because the result line came."""
    fixture = tmp_path / "structured_no_value.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s-nv", "tools": ["StructuredOutput"]}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "struct_1", "name": "StructuredOutput", "input": {}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"prime": true}'}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "", "session_id": "s-nv"}),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(fixture))
    llm = ClaudeCodeLLM(_provider(fake_claude))
    events = [
        e async for e in llm.stream(
            model="sonnet", messages=[_user("answer in json")],
            response_format={"type": "object", "properties": {"prime": {"type": "boolean"}}},
        )
    ]
    assert not any(isinstance(e, TextDelta) for e in events), "no structured_output and no buffered text: no answer"
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "stop"
    assert not any(isinstance(e, ChatError) for e in events)


async def test_events_after_done_are_dropped(tmp_path):
    """Once a Done that is not held is yielded, the step ends at once (spec 6.2
    step 9 v1.5): the CLI is killed, and a second message the CLI would have
    generated is never read. Nothing after the Done is streamed."""
    fixture = tmp_path / "two_messages_no_result.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            # a second message after the Done (no result line before it)
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "second"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
        ]) + "\n",
        encoding="utf-8",
    )
    script = tmp_path / "two-messages"
    script.write_text(f"#!/bin/sh\ncat {fixture}\n", encoding="utf-8")
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script)))
    events = [e async for e in llm.stream(model="sonnet", messages=[_user("hi")])]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "hello", "nothing after the Done is streamed"
    assert sum(1 for e in events if isinstance(e, Done)) == 1
    assert not any(isinstance(e, ChatError) for e in events)


async def test_done_without_response_format_ends_the_step_promptly(tmp_path):
    """Spec 6.2 step 9 v1.5: a Done that is not held (no response_format) ends
    the step at once -- the CLI is killed, never left running to spend plan or
    block on a bridge-held call. The measured shape: a complete tool block, then
    a max_tokens stop, then a CLI that waits for the tool result it never gets."""
    import time

    fixture = tmp_path / "tool_then_max_tokens.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1",
                        "mcp_servers": [{"name": "p", "status": "connected", "source": "dynamic"}]}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_01", "name": "mcp__p__fs__read", "input": {}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path": "/etc/hosts"}'}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            # the CLI now waits for the tool result it will never get
        ]) + "\n",
        encoding="utf-8",
    )
    script = tmp_path / "tool-then-max-tokens"
    script.write_text(f"#!/bin/sh\ncat {fixture}\nsleep 60\n", encoding="utf-8")
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script), request_timeout=60.0))
    t0 = time.monotonic()
    events = [e async for e in llm.stream(model="sonnet", messages=[_user("read /etc/hosts")], tools=[_fs_tool()])]
    elapsed = time.monotonic() - t0
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "max_tokens"
    assert not any(isinstance(e, ChatError) for e in events)
    # Well under the 60 s stall window: the step ends at the Done (plus the
    # bounded kill/reap cleanup), it does not wait for a line that never comes.
    assert elapsed < 30.0, f"the step must end at the Done, not wait out the stall window ({elapsed:.1f} s)"


async def test_done_after_a_primer_tool_call_ends_the_step_promptly(tmp_path):
    """Spec 6.2 step 9 v1.6: a response_format step that has emitted a primer
    tool call must NOT wait for the result line (the bridge holds the call, so
    the CLI never writes one): the message's Done ends the step at once
    whatever its stop reason (here max_tokens after a complete tool block),
    and the CLI is killed."""
    import time

    fixture = tmp_path / "tool_then_max_tokens_schema.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1",
                        "mcp_servers": [{"name": "p", "status": "connected", "source": "dynamic"}]}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_01", "name": "mcp__p__fs__read", "input": {}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path": "/etc/hosts"}'}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            # the CLI now blocks on the bridge-held call; it never writes a result line
        ]) + "\n",
        encoding="utf-8",
    )
    script = tmp_path / "tool-then-max-tokens-schema"
    script.write_text(f"#!/bin/sh\ncat {fixture}\nsleep 60\n", encoding="utf-8")
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script), request_timeout=60.0))
    t0 = time.monotonic()
    events = [
        e async for e in llm.stream(
            model="sonnet", messages=[_user("read /etc/hosts")],
            tools=[_fs_tool()],
            response_format={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    ]
    elapsed = time.monotonic() - t0
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "max_tokens"
    assert not any(isinstance(e, ChatError) for e in events)
    # Well under the 60 s stall window: the step ends at the Done (plus the
    # bounded kill/reap cleanup), it does not wait for a result that never comes.
    assert elapsed < 30.0, f"the step must end at the Done, not wait out the stall window ({elapsed:.1f} s)"


async def test_held_done_from_a_rejected_message_does_not_block_the_early_exit(tmp_path):
    """Spec 6.2 step 9 v1.6: a Done held from an earlier message (StructuredOutput
    call alone, rejected by the CLI and retried) must not block the early exit
    once a later message emits a primer tool call: the next Done ends the step
    at once whatever its stop reason, so exactly one Done is ever yielded and
    the step does not wait out the stall window for a result that never comes."""
    import time

    msg1_structured = [
        json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}}),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_so", "name": "StructuredOutput", "input": {}}}}),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"answer": "x"}'}}}),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}),
        json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}}),
        json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
    ]
    fixture = tmp_path / "held_then_tool_block.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s-stale",
                        "mcp_servers": [{"name": "p", "status": "connected", "source": "dynamic"}]}),
            *msg1_structured,
            # the CLI rejects the StructuredOutput input; the model retries
            json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_so",
                 "content": "Error: input rejected", "is_error": True}]}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_01", "name": "mcp__p__fs__read", "input": {}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path": "/etc/hosts"}'}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            # the CLI now blocks on the bridge-held primer call; it never writes a result line
        ]) + "\n",
        encoding="utf-8",
    )
    script = tmp_path / "held-then-tool-block"
    script.write_text(f"#!/bin/sh\ncat {fixture}\nsleep 60\n", encoding="utf-8")
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script), request_timeout=60.0))
    t0 = time.monotonic()
    events = [
        e async for e in llm.stream(
            model="sonnet", messages=[_user("read /etc/hosts")],
            tools=[_fs_tool()],
            response_format={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    ]
    elapsed = time.monotonic() - t0
    dones = [e for e in events if isinstance(e, Done)]
    assert len(dones) == 1, f"the step yields exactly one Done (the held one is superseded), got {len(dones)}"
    assert dones[0].stop_reason == "max_tokens"
    assert any(isinstance(e, ToolCallStart) for e in events)
    assert not any(isinstance(e, ChatError) for e in events)
    # Well under the 60 s stall window: the stale held Done must not keep the
    # step waiting for a result line that never comes.
    assert elapsed < 30.0, f"the step must end at the Done, not wait out the stall window ({elapsed:.1f} s)"


async def test_cli_messaging_socket_is_removed_after_the_kill(tmp_path):
    """Spec 6.8 v1.6: after the kill, the CLI's messaging socket (named in its
    init line) is unlinked -- only when its NORMALISED path is under
    /tmp/cc-socks/; a socket anywhere else (including a `..` that escapes the
    directory) is left alone."""
    import uuid

    socket_path = Path("/tmp/cc-socks") / f"cc-test-{uuid.uuid4().hex}.sock"
    socket_path.parent.mkdir(exist_ok=True)
    socket_path.write_text("")
    other = tmp_path / "elsewhere.sock"
    other.write_text("")
    # Named THROUGH the directory with a .. that resolves OUT of it.
    outside_name = f"cc-out-{uuid.uuid4().hex}.sock"
    outside_named = "/tmp/cc-socks/../" + outside_name
    outside = Path("/tmp") / outside_name
    outside.write_text("")
    try:
        async def run_one(label: str, init_socket: str) -> None:
            fixture = tmp_path / f"{label}.ndjson"
            fixture.write_text(
                "\n".join([
                    json.dumps({"type": "system", "subtype": "init", "session_id": "s1",
                                "messaging_socket_path": init_socket}),
                    json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}}),
                    json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}}),
                    json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}}),
                    json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
                ]) + "\n",
                encoding="utf-8",
            )
            script = tmp_path / f"{label}-cli"
            script.write_text(f"#!/bin/sh\ncat {fixture}\n", encoding="utf-8")
            script.chmod(0o755)
            llm = ClaudeCodeLLM(_provider(str(script)))
            events = [e async for e in llm.stream(model="sonnet", messages=[_user("hi")])]
            assert isinstance(events[-1], Done)

        await run_one("socks", str(socket_path))
        assert not socket_path.exists(), "the /tmp/cc-socks socket is removed after the kill"
        await run_one("elsewhere", str(other))
        assert other.exists(), "a socket outside /tmp/cc-socks/ is left alone"
        await run_one("dotdot", outside_named)
        assert outside.exists(), "a path that normpaths OUT of /tmp/cc-socks/ is left alone"
    finally:
        socket_path.unlink(missing_ok=True)
        outside.unlink(missing_ok=True)


async def test_step_closes_when_cancelled_during_the_stdin_join(tmp_path, monkeypatch):
    """A second cancel that lands in the stdin join (the stream's finally) must
    not skip the bridge step close: the step close is nested in a try/finally
    around the join."""
    real_wait = asyncio.wait

    async def wait_raises_without_timeout(futures, *args, **kwargs):
        # The adapter's join call has no timeout; shielded's cleanup does, so
        # only the join raises the simulated second cancel.
        if "timeout" not in kwargs:
            raise asyncio.CancelledError()
        return await real_wait(futures, *args, **kwargs)

    monkeypatch.setattr(asyncio, "wait", wait_raises_without_timeout)
    from primer.llm.claude_code.bridge import get_bridge

    bridge = await get_bridge()
    tokens_before = set(bridge._steps)
    fixture = tmp_path / "cancel-join.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
        ]) + "\n",
        encoding="utf-8",
    )
    script = tmp_path / "cancel-join"
    script.write_text(f"#!/bin/sh\ncat {fixture}\n", encoding="utf-8")
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script)))
    with pytest.raises(asyncio.CancelledError):
        [e async for e in llm.stream(model="sonnet", messages=[_user("hi")], tools=[_fs_tool()])]
    assert set(bridge._steps) <= tokens_before, "the step was closed despite the cancel in the join"


async def test_oversize_stdout_line_is_classified_not_raw(tmp_path, monkeypatch):
    """A line beyond the readline limit (monkeypatched small here: the 300 KB
    C1 test covers the real limit) is a classified ProviderError, never a raw
    ValueError."""
    monkeypatch.setattr("primer.llm.claude_code.process._STREAM_LINE_LIMIT", 1024)
    fixture = tmp_path / "oversize_pre.ndjson"
    # a 2 KiB first line: beyond the 1024-byte readline limit -> ValueError
    fixture.write_text("x" * 2048 + "\n", encoding="utf-8")
    script = tmp_path / "oversize-pre"
    script.write_text(f"#!/bin/sh\ncat {fixture}\n", encoding="utf-8")
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script)))
    events = [e async for e in llm.stream(model="sonnet", messages=[_user("hi")])]
    assert isinstance(events[-1], ChatError)
    assert events[-1].fatal and events[-1].code == "claude_code_step_failed"


async def test_stall_timeout_after_done_ends_the_stream_silently(tmp_path):
    """A stall or total timeout while waiting for the result line AFTER Done was
    yielded must be a log plus return, never an Error after the terminal event.
    (Since spec 6.2 step 9 v1.5 the step breaks and kills at a non-held Done, so
    the stall is never observed here: the stream still must end at Done, quietly.)"""
    import time

    fixture = tmp_path / "done_then_hang.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            # the result line never comes: the CLI hangs here
        ]) + "\n",
        encoding="utf-8",
    )
    script = tmp_path / "done-then-hang"
    script.write_text(f"#!/bin/sh\ncat {fixture}\nsleep 60\n", encoding="utf-8")
    script.chmod(0o755)
    # A wide stall window: the step kills at the non-held Done and never waits
    # it out, so the width costs no time; a 0.3 s window could be spent in
    # scheduling alone on a loaded host and trip the stall before any event.
    llm = ClaudeCodeLLM(_provider(str(script), request_timeout=30.0))
    t0 = time.monotonic()
    events = [e async for e in llm.stream(model="sonnet", messages=[_user("hi")])]
    elapsed = time.monotonic() - t0
    # The stream ends at Done; the late stall timeout was logged, not raised.
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "stop"
    assert not any(isinstance(e, ChatError) for e in events)
    # Proves the step ended at the Done (plus the bounded kill/reap cleanup),
    # not after waiting out the 30 s stall window.
    assert elapsed < 20.0, f"the step must end at the Done, not wait out the stall window ({elapsed:.1f} s)"


async def test_a_cli_that_never_reads_stdin_fails_on_connect_timeout(tmp_path):
    """The tail is written concurrently with the stdout reads: a CLI that never reads
    stdin (a tail beyond the pipe buffer) must trip the connect timeout, not hang the
    step forever holding the llm slot."""
    script = tmp_path / "never-reads"
    script.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script), request_timeout=0.3, connect_timeout=0.5))
    # A 200 KB tail leaves >64 KiB beyond the pipe buffer: the old synchronous
    # write+drain pauses on the high-water mark and hangs forever (the CLI never
    # reads), holding the llm slot.
    messages = [Message(role="user", parts=[TextPart(text="y" * (200 * 1024))])]
    with pytest.raises(ProviderTimeoutError) as exc:
        async for _ in llm.stream(model="sonnet", messages=messages):
            pass
    assert exc.value.code == "connect_timeout"


async def test_a_cli_that_exits_at_startup_is_cli_failed_not_broken_pipe(tmp_path):
    """A CLI that exits at startup breaks the stdin pipe: the BrokenPipeError is routed
    to the EOF/stderr-quoting claude_code_cli_failed path, never raised raw."""
    script = tmp_path / "dies-at-startup"
    script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script)))
    messages = [Message(role="user", parts=[TextPart(text="y" * (200 * 1024))])]
    with pytest.raises(ProviderError) as exc:
        async for _ in llm.stream(model="sonnet", messages=messages):
            pass
    assert exc.value.code == "claude_code_cli_failed"
    assert "exited without a result (code 1)" in str(exc.value)


async def test_crash_stderr_tail_is_scrubbed(tmp_path):
    """Spec 6.6: the stderr tail quoted into a failing result goes through
    describe_failure, so a secret the CLI echoed is redacted."""
    fixture = tmp_path / "empty.ndjson"
    fixture.write_text("", encoding="utf-8")
    secret = "sk-crash-echo-0123456789abcdef"
    script = tmp_path / "crashes"
    script.write_text(
        f"#!/bin/sh\necho 'fatal: Authorization: Bearer {secret}' >&2\nexit 2\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script)))
    with pytest.raises(ProviderError) as exc:
        async for _ in llm.stream(model="sonnet", messages=[_user("hi")]):
            pass
    assert exc.value.code == "claude_code_cli_failed"
    assert "fatal: Authorization: Bearer" in exc.value.message  # the tail is quoted
    assert secret not in exc.value.message
    assert "[REDACTED]" in exc.value.message


async def test_missing_binary_raises_config_error():
    llm = ClaudeCodeLLM(_provider("/nonexistent/claude-never-installed"))
    with pytest.raises(ConfigError) as exc:
        async for _ in llm.stream(model="sonnet", messages=[_user("hi")]):
            pass
    assert "claude CLI not found on" in str(exc.value)
    assert "/nonexistent/claude-never-installed" in str(exc.value)


async def test_spawn_failure_is_a_classified_provider_error(tmp_path):
    """Spec 6.6 v1.5: an OSError at spawn (a shebang whose interpreter does not
    exist: the file passes the binary pre-check, exec fails with ENOENT/
    ENOEXEC) is a classified ProviderError, never a raw OSError."""
    script = tmp_path / "bad-claude"
    script.write_text("#!/nonexistent-interpretor\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script)))
    with pytest.raises(ProviderError) as exc:
        async for _ in llm.stream(model="sonnet", messages=[_user("hi")]):
            pass
    assert type(exc.value) is ProviderError
    assert exc.value.code == "claude_code_spawn_failed"
    assert "could not be spawned" in exc.value.message


async def test_bridge_not_connected_is_a_retryable_error(fake_claude, tmp_path, monkeypatch):
    """Spec 6.6 v1.5: tools were registered but the CLI's init line does not
    report the bridge (p) as connected: the model would run without its tools,
    so the step fails with a RETRYABLE error (the worker backs off and
    re-enqueues the session)."""
    fixture = tmp_path / "bridge_failed.ndjson"
    fixture.write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": "s-b",
                    "tools": ["mcp__p__fs__read"],
                    "mcp_servers": [{"name": "p", "status": "failed", "source": "dynamic"}]}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(fixture))
    llm = ClaudeCodeLLM(_provider(fake_claude))
    with pytest.raises(TransientError) as exc:
        async for _ in llm.stream(model="sonnet", messages=[_user("hi")], tools=[_fs_tool()]):
            pass
    assert exc.value.code == "claude_code_bridge_not_connected"
    assert is_retryable(exc.value)


async def test_logged_out_raises_authentication_error(fake_claude, tmp_path, monkeypatch):
    _stream_env(monkeypatch, tmp_path, "logged_out")
    llm = ClaudeCodeLLM(_provider(fake_claude))
    with pytest.raises(AuthenticationError):
        async for _ in llm.stream(model="sonnet", messages=[_user("hi")]):
            pass


async def test_effort_and_json_schema_flags(fake_claude, tmp_path, monkeypatch):
    record = _stream_env(monkeypatch, tmp_path, "text_with_tool")
    llm = ClaudeCodeLLM(_provider(fake_claude))
    events = [
        e async for e in llm.stream(
            model="sonnet",
            messages=[_user("answer in json")],
            extended={"effort": "high"},
            response_format={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    ]
    assert events[-1].stop_reason == "stop"
    rec = json.loads(record.read_text(encoding="utf-8"))
    assert rec["argv"][rec["argv"].index("--effort") + 1] == "high"
    assert "--json-schema" in rec["argv"]


async def test_structured_result_without_value_uses_the_buffered_text(fake_claude, tmp_path, monkeypatch):
    """Spec 6.7 v1.5: a successful result line WITHOUT structured_output falls
    back to the buffered text: the answer is the streamed text as ONE TextDelta."""
    fixture = tmp_path / "structured_prose.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s-prose"}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "the answer is "}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "21"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "the answer is 21", "session_id": "s-prose"}),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(fixture))
    llm = ClaudeCodeLLM(_provider(fake_claude))
    events = [
        e async for e in llm.stream(
            model="sonnet", messages=[_user("answer in json")],
            response_format={"type": "object", "properties": {"answer": {"type": "integer"}}},
        )
    ]
    text = [e for e in events if isinstance(e, TextDelta)]
    assert len(text) == 1
    assert text[0].text == "the answer is 21", "the buffered text is the answer, as one delta"
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "stop"


async def test_prose_then_structured_call_emits_only_the_result_value(fake_claude, tmp_path, monkeypatch):
    """Spec 6.7 v1.5 (problem a): the model streams prose and ends end_turn
    BEFORE calling StructuredOutput. The first message's Done is held too (with
    response_format set, every Done is held) and the prose is buffered, never
    streamed; the answer is the result's structured_output and only that."""
    fixture = tmp_path / "prose_then_structured.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s-p", "tools": ["StructuredOutput"]}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 5, "output_tokens": 0}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "let me think about it"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m2", "usage": {"input_tokens": 9, "output_tokens": 0}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "struct_1", "name": "StructuredOutput", "input": {}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"prime": true}'}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": {"prime": True}, "session_id": "s-p"}),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(fixture))
    llm = ClaudeCodeLLM(_provider(fake_claude))
    events = [
        e async for e in llm.stream(
            model="sonnet", messages=[_user("answer in json")],
            response_format={"type": "object", "properties": {"prime": {"type": "boolean"}}},
        )
    ]
    text = [e for e in events if isinstance(e, TextDelta)]
    assert len(text) == 1, "the prose is buffered and dropped, not streamed"
    assert json.loads(text[0].text) == {"prime": True}, "the answer is the validated structured_output"
    assert "let me think" not in text[0].text
    dones = [e for e in events if isinstance(e, Done)]
    assert len(dones) == 1 and dones[0].stop_reason == "stop"


async def test_cli_exit_after_held_done_without_result_line_is_cli_failed(tmp_path):
    """Spec 6.7 v1.5 (problem b): a CLI that exits after a held Done with no
    result line and no answer (no buffered text, no structured_output) is an
    error, not an empty Done(stop)."""
    fixture = tmp_path / "structured_no_result.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s-x", "tools": ["StructuredOutput"]}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "struct_1", "name": "StructuredOutput", "input": {}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"prime": true}'}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            # the CLI exits here; the result line never comes
        ]) + "\n",
        encoding="utf-8",
    )
    script = tmp_path / "structured-no-result"
    script.write_text(f"#!/bin/sh\ncat {fixture}\n", encoding="utf-8")
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script)))
    with pytest.raises(ProviderError) as exc:
        async for _ in llm.stream(
            model="sonnet", messages=[_user("answer in json")],
            response_format={"type": "object", "properties": {"prime": {"type": "boolean"}}},
        ):
            pass
    assert exc.value.code == "claude_code_cli_failed"


async def test_cli_exit_after_held_done_emits_the_buffered_text(tmp_path):
    """Spec 6.7 v1.5: a CLI that exits after a held Done with no result line but
    WITH buffered text: the buffered text is the answer (one TextDelta + the
    held Done), not an error."""
    fixture = tmp_path / "prose_no_result.ndjson"
    fixture.write_text(
        "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "s-y"}),
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi "}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "there"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}),
            # the CLI exits here; the result line never comes
        ]) + "\n",
        encoding="utf-8",
    )
    script = tmp_path / "prose-no-result"
    script.write_text(f"#!/bin/sh\ncat {fixture}\n", encoding="utf-8")
    script.chmod(0o755)
    llm = ClaudeCodeLLM(_provider(str(script)))
    events = [
        e async for e in llm.stream(
            model="sonnet", messages=[_user("hi")],
            response_format={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    ]
    text = [e for e in events if isinstance(e, TextDelta)]
    assert len(text) == 1 and text[0].text == "hi there"
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "stop"
    assert not any(isinstance(e, ChatError) for e in events)


async def test_count_tokens_unavailable():
    llm = ClaudeCodeLLM(_provider("claude"))
    with pytest.raises(TokenCounterUnavailable):
        await llm.count_tokens(model="sonnet", messages=[_user("hi")])


def test_claude_code_extended_maps_effort():
    assert claude_code_extended(ModelProfileConfig(reasoning=None)) == {}
    assert claude_code_extended(ModelProfileConfig(reasoning=ReasoningLevel.HIGH))["effort"] == "high"
    assert claude_code_extended(ModelProfileConfig(reasoning=ReasoningLevel.MEDIUM))["effort"] == "medium"
    for level in (ReasoningLevel.OFF, ReasoningLevel.MINIMAL, ReasoningLevel.LOW):
        assert claude_code_extended(ModelProfileConfig(reasoning=level))["effort"] == "low"


def test_registry_builds_claude_code():
    from primer.api.registries.provider_registry import _build_default_llm_factory

    factory = _build_default_llm_factory()
    adapter = factory(_provider("claude"))
    assert isinstance(adapter.inner, ClaudeCodeLLM)
