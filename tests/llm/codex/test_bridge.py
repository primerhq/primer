import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from primer.coordinator.in_memory import InMemoryRateLimiter
from primer.llm.codex.adapter import CodexFoundationLLM
from primer.llm.codex.boundary import BoundaryError, BridgeConfig
from primer.llm.codex.bridge import ModelOnlyBridge
from primer.llm.codex.harness import RecordedChannel, RecordedPolicy
from primer.llm.codex.request import alias, prepare_request
from primer.model.chat import Message, TextPart, Tool, ToolCallPart, ToolResultPart
from primer.model.except_ import TokenCounterUnavailable, UnsupportedContentError


def message(text="hello"):
    return Message(role="user", parts=[TextPart(text=text)])


def tool(name="files/read"):
    return Tool(id=name, toolset_id="test-tools", description="Read a file", args_schema={"type": "object", "properties": {"path": {"type": "string"}}})


def frames(calls=(), response_id="r1", text="hello"):
    events = [{"type": "response.created", "response": {"id": response_id}},
              {"type": "response.output_text.delta", "response_id": response_id, "delta": text, "output_index": 0}]
    output = []
    for index, name in enumerate(calls):
        item = {"id": f"fc{index}", "type": "function_call", "namespace": "primer", "name": alias(name),
                "call_id": f"call{index}", "arguments": '{"path":"a.txt"}'}
        output.append(item)
        events.extend([{"type": "response.output_item.added", "output_index": index + 1, "item": {**item, "arguments": ""}},
                       {"type": "response.function_call_arguments.delta", "item_id": item["id"], "delta": '{"path":'},
                       {"type": "response.output_item.done", "output_index": index + 1, "item": item}])
    events.append({"type": "response.completed", "response": {"id": response_id, "status": "completed", "output": output,
                   "usage": {"input_tokens": 7, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 2}}}})
    return events


def bridge(events=None, *, config=None, authority=None, factory=None):
    channels = []
    def make(wire):
        channel = RecordedChannel(events if events is not None else frames())
        channels.append(channel)
        return channel
    instance = ModelOnlyBridge(authority=authority or RecordedPolicy("model"), account="offline",
                               transport_factory=factory or make, config=config)
    return instance, channels


@pytest.mark.asyncio
async def test_parallel_calls_close_before_exposing_calls_and_done():
    instance, channels = bridge(frames(["files/read", "search"]))
    output = []
    async for event in instance.stream(prepare_request("model", [message()], [tool(), tool("search")])):
        if event.type in {"tool_call_start", "tool_call_end", "done"}:
            assert channels[0].closed
        output.append(event)
    assert [x.name for x in output if x.type == "tool_call_start"] == ["files/read", "search"]
    assert [x.arguments for x in output if x.type == "tool_call_end"] == [{"path": "a.txt"}] * 2
    assert output[-1].stop_reason == "tool_use"
    assert len([e for e in output if e.type == "done"]) == 1
    assert output[-2].cached_input_tokens == 2
    assert not instance._active


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["local_shell_call", "shell_call", "apply_patch_call", "mcp_call", "web_search_call", "computer_call"])
async def test_actual_bridge_refuses_native_outputs_and_closes(kind):
    events = frames()
    events.insert(1, {"type": "response.output_item.added", "item": {"id": "native", "type": kind}})
    instance, channels = bridge(events)
    with pytest.raises(BoundaryError):
        _ = [e async for e in instance.stream(prepare_request("model", [message()]))]
    assert channels[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unadvertised", "changed", "missing", "duplicate", "cross_response", "truncated", "error", "array_args"])
async def test_protocol_failure_never_exposes_executable_call(failure):
    events = json.loads(json.dumps(frames(["files/read"])))
    if failure == "unadvertised":
        events[4]["item"]["name"] = "shell"
    elif failure == "changed":
        events[-1]["response"]["output"][0]["arguments"] = '{"path":"changed"}'
    elif failure == "missing":
        del events[4]
    elif failure == "duplicate":
        events.insert(5, events[4])
    elif failure == "cross_response":
        events[1]["response_id"] = "another-step"
    elif failure == "truncated":
        events.pop()
    elif failure == "error":
        events[-1] = {"type": "error", "message": "sensitive upstream text"}
    else:
        events[4]["item"]["arguments"] = "[]"
    instance, channels = bridge(events)
    emitted = []
    with pytest.raises(BoundaryError):
        async for event in instance.stream(prepare_request("model", [message()], [tool()])):
            emitted.append(event)
    assert not any(e.type in {"tool_call_start", "tool_call_end", "done"} for e in emitted)
    assert channels[0].closed


@pytest.mark.asyncio
async def test_policy_refresh_after_request_cannot_expand_capabilities():
    authority = RecordedPolicy("model")
    class Changing(RecordedChannel):
        async def __anext__(self):
            event = await super().__anext__()
            authority.snapshot = replace(authority.snapshot, required_native_effects=frozenset({"cloud_mcp"}))
            return event
    channel = Changing(frames())
    instance, _ = bridge(authority=authority, factory=lambda _: channel)
    with pytest.raises(BoundaryError):
        _ = [e async for e in instance.stream(prepare_request("model", [message()]))]
    assert channel.closed
    assert not instance._active


@pytest.mark.asyncio
async def test_policy_change_even_compatible_requires_fresh_admission():
    authority = RecordedPolicy("model")
    class Changing(RecordedChannel):
        async def __anext__(self):
            event = await super().__anext__()
            authority.snapshot = replace(authority.snapshot, revision="new")
            return event
    channel = Changing(frames())
    instance, _ = bridge(authority=authority, factory=lambda _: channel)
    with pytest.raises(BoundaryError, match="changed"):
        _ = [e async for e in instance.stream(prepare_request("model", [message()]))]
    assert channel.closed


@pytest.mark.asyncio
async def test_cancel_during_open_and_repeat_during_cleanup_drain_owned_channel():
    started, closing, finish_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Held(RecordedChannel):
        async def __anext__(self):
            started.set()
            await asyncio.Event().wait()
        async def aclose(self):
            closing.set()
            await finish_close.wait()
            await super().aclose()
    channel = Held([])
    instance, _ = bridge(factory=lambda _: channel)
    async def consume():
        return [e async for e in instance.stream(prepare_request("model", [message()]))]
    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.wait_for(closing.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert channel.closed and not instance._active


@pytest.mark.asyncio
async def test_deadline_and_cleanup_failure_never_report_success():
    class Stalled(RecordedChannel):
        async def __anext__(self):
            await asyncio.Event().wait()
    channel = Stalled([])
    instance, _ = bridge(factory=lambda _: channel, config=BridgeConfig(deadline_seconds=.02))
    with pytest.raises(TimeoutError):
        _ = [e async for e in instance.stream(prepare_request("model", [message()]))]
    assert channel.closed
    class FailedClose(RecordedChannel):
        async def aclose(self):
            raise RuntimeError("cleanup failed")
    channel2 = FailedClose(frames(["files/read"]))
    instance, _ = bridge(factory=lambda _: channel2)
    emitted = []
    with pytest.raises(RuntimeError):
        async for event in instance.stream(prepare_request("model", [message()], [tool()])):
            emitted.append(event)
    assert not any(e.type in {"tool_call_end", "done"} for e in emitted)
    assert not instance._active


@pytest.mark.asyncio
async def test_close_cancels_active_step_and_refuses_new_step():
    started = asyncio.Event()
    class Held(RecordedChannel):
        async def __anext__(self):
            started.set()
            await asyncio.Event().wait()
    channel = Held([])
    instance, _ = bridge(factory=lambda _: channel)
    async def consume():
        return [e async for e in instance.stream(prepare_request("model", [message()]))]
    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 1)
    await instance.aclose()
    assert task.cancelled() and channel.closed and not instance._active
    with pytest.raises(BoundaryError, match="closed"):
        await consume()


@pytest.mark.asyncio
async def test_concurrent_steps_have_no_history_or_response_leakage():
    received = []
    channels = []
    def factory(wire):
        received.append(wire)
        text = wire["input"][0]["content"][0]["text"]
        channel = RecordedChannel(frames(response_id=text, text=text))
        channels.append(channel)
        return channel
    instance, _ = bridge(factory=factory)
    async def consume(text):
        return [e async for e in instance.stream(prepare_request("model", [message(text)]))]
    results = await asyncio.gather(consume("first-private"), consume("second-private"))
    assert [[e.text for e in r if e.type == "text_delta"] for r in results] == [["first-private"], ["second-private"]]
    assert all(c.closed for c in channels) and len(received) == 2
    assert all(len(w["input"]) == 1 for w in received)


def test_replay_preserves_tool_round_and_does_not_reenable_old_tools():
    req = prepare_request("model", [message(),
        Message(role="assistant", parts=[ToolCallPart(id="a", name="old-tool", arguments={"x": 1})]),
        Message(role="tool", parts=[ToolResultPart(id="a", output="answer")]), message("continue")], [tool()])
    wire = req.wire()
    assert [i["type"] for i in wire["input"]] == ["message", "function_call", "function_call_output", "message"]
    assert wire["input"][1]["name"] == alias("old-tool")
    assert wire["tools"][0]["tools"][0]["name"] == alias("files/read")
    assert wire["store"] is False and wire["stream"] is True


@pytest.mark.parametrize("parts", [
    [Message(role="tool", parts=[ToolResultPart(id="orphan", output="x")])],
    [Message(role="assistant", parts=[ToolCallPart(id="a", name="t", arguments={})])],
    [Message(role="user", parts=[ToolCallPart(id="a", name="t", arguments={})])],
])
def test_invalid_history_refused(parts):
    with pytest.raises(BoundaryError):
        prepare_request("model", parts)


@pytest.mark.asyncio
async def test_foundation_adapter_error_sanitization_and_slot_release():
    instance, _ = bridge([{"type": "error", "secret": "never echo"}])
    limiter = InMemoryRateLimiter()
    adapter = CodexFoundationLLM(instance, provider_id="test", rate_limiter=limiter)
    results = [e async for e in adapter.stream(model="model", messages=[message()])]
    assert len(results) == 1 and results[0].type == "error" and results[0].fatal
    assert "never echo" not in results[0].message
    async with await limiter.acquire("llm:test", max_concurrency=1):
        pass
    with pytest.raises(TokenCounterUnavailable):
        await adapter.count_tokens(model="model", messages=[message()])
    with pytest.raises(UnsupportedContentError):
        _ = [e async for e in adapter.stream(model="model", messages=[message()], extended={"config": {}})]


def test_actual_executable_harness_without_credentials_or_native_process(tmp_path):
    transcript = tmp_path / "synthetic.json"
    transcript.write_text(json.dumps(frames(["files/read"])))
    req = prepare_request("model", [message()], [tool()])
    messages = [{"id": 1, "method": "step/start", "params": {"request": req.wire(), "aliases": req.aliases, "config": {}}},
                {"id": 2, "method": "command/exec", "params": {"command": "touch must-not-exist"}},
                {"id": 3, "method": "bridge/status", "params": {}}]
    env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
           "HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, "-B", "-m", "primer.llm.codex.harness", "--transcript", str(transcript)],
                          input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=30)
    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(line) for line in proc.stdout.splitlines()]
    assert rows[0]["result"]["events"][-1]["stop_reason"] == "tool_use"
    assert rows[1]["error"]["code"] == "codex_boundary_refused"
    assert rows[2]["result"] == {"mode": "offline", "live_enabled": False}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["synthetic.json"]


@pytest.mark.asyncio
async def test_close_timeout_and_capacity_are_bounded():
    entered, release = asyncio.Event(), asyncio.Event()
    class Held(RecordedChannel):
        async def __anext__(self):
            entered.set()
            await release.wait()
            return await super().__anext__()
    channel = Held(frames())
    instance, _ = bridge(factory=lambda _: channel, config=BridgeConfig(max_active_steps=1))
    async def consume():
        return [e async for e in instance.stream(prepare_request("model", [message()]))]
    first = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(BoundaryError, match="capacity"):
        await consume()
    release.set()
    assert (await first)[-1].type == "done"
    class SlowClose(RecordedChannel):
        async def aclose(self):
            await asyncio.Event().wait()
    instance, _ = bridge(factory=lambda _: SlowClose(frames()), config=BridgeConfig(cleanup_seconds=.02))
    with pytest.raises(TimeoutError):
        _ = [e async for e in instance.stream(prepare_request("model", [message()]))]
    assert not instance._active


@pytest.mark.asyncio
async def test_cancel_in_policy_never_constructs_transport():
    entered = asyncio.Event()
    class Policy:
        async def current(self):
            entered.set()
            await asyncio.Event().wait()
    instance, _ = bridge(authority=Policy(), factory=lambda _: pytest.fail("constructed"))
    async def consume():
        return [e async for e in instance.stream(prepare_request("model", [message()]))]
    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not instance._active


def test_cross_loop_refused_and_snapshot_cannot_be_mutated():
    source = tool()
    req = prepare_request("model", [Message(role="system", parts=[TextPart(text="policy")])], [source])
    source.args_schema["type"] = "array"
    assert req.wire()["tools"][0]["tools"][0]["parameters"]["type"] == "object"
    assert req.wire()["input"][0]["role"] == "developer"
    instance, _ = bridge()
    async def consume():
        return [e async for e in instance.stream(req)]
    assert asyncio.run(consume())[-1].type == "done"
    with pytest.raises(BoundaryError, match="event loop"):
        asyncio.run(consume())


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,calls", [("none", ["files/read"]), ("required", []), ("files/read", [])])
async def test_tool_choice_enforced(choice, calls):
    instance, channels = bridge(frames(calls))
    with pytest.raises(BoundaryError):
        _ = [e async for e in instance.stream(prepare_request("model", [message()], [tool()], tool_choice=choice))]
    assert channels[0].closed


def test_current_plan_wire_refuses_unsupported_token_cap_and_preserves_tool_ids():
    with pytest.raises(UnsupportedContentError, match="max_output_tokens"):
        prepare_request("model", [message()], max_output_tokens=123)
    first, second = tool("tool-a"), tool("tool-b")
    first.description = second.description = "same human description"
    req = prepare_request("model", [message()], [first, second])
    assert dict(req.aliases) == {alias("tool-a"): "tool-a", alias("tool-b"): "tool-b"}


@pytest.mark.asyncio
async def test_breaking_consumer_closes_channel_without_terminal_leak():
    instance, channels = bridge()
    stream = instance.stream(prepare_request("model", [message()]))
    assert (await anext(stream)).type == "text_delta"
    await stream.aclose()
    assert channels[0].closed and not instance._active


def test_harness_under_actual_python_effect_tripwires(tmp_path):
    """Audit hooks observe the actual harness, not a mocked capability claim.

    This proves only the offline executable's tested paths. It is not a Linux
    sandbox or a qualification of any future authenticated transport.
    """
    transcript = tmp_path / "synthetic.json"
    transcript.write_text(json.dumps(frames()))
    req = prepare_request("model", [message()])
    inputs = [{"id": 1, "method": "step/start", "params": {"request": req.wire(), "aliases": [], "config": {}}},
              {"id": 2, "method": "step/start", "params": {"request": req.wire(), "aliases": [], "config": {"hooks": ["touch marker"]}}},
              {"id": 3, "method": "process/spawn", "params": {"command": "touch marker"}}]
    code = '''
import os, runpy, sys

def audit(event, args):
    if event in {'subprocess.Popen', 'os.system', 'os.exec', 'socket.connect', 'socket.bind'}:
        raise RuntimeError('forbidden native effect')
    if event == 'open':
        path, mode, flags = args
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise RuntimeError('forbidden file write')
sys.addaudithook(audit)
# A real positive control confirms the file-write tripwire is live.
try:
    open('must-not-exist', 'w')
except RuntimeError:
    pass
else:
    raise AssertionError('inactive tripwire')
sys.argv = ['harness', '--transcript', sys.argv[1]]
runpy.run_module('primer.llm.codex.harness', run_name='__main__')
'''
    env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
           "HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, "-B", "-c", code, str(transcript)],
                          input="".join(json.dumps(m) + "\n" for m in inputs), capture_output=True,
                          text=True, env=env, cwd=tmp_path, timeout=30)
    assert proc.returncode == 0, proc.stderr
    output = [json.loads(line) for line in proc.stdout.splitlines()]
    assert output[0]["result"]["events"][-1]["type"] == "done"
    assert all("error" in row for row in output[1:])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["synthetic.json"]


@pytest.mark.asyncio
async def test_policy_revoked_during_cleanup_withholds_all_calls():
    policy = RecordedPolicy("model")
    class RevokeOnClose(RecordedChannel):
        async def aclose(self):
            await super().aclose()
            policy.snapshot = replace(policy.snapshot, suspended=True)
    channel = RevokeOnClose(frames(["files/read"]))
    instance, _ = bridge(authority=policy, factory=lambda _: channel)
    events = []
    with pytest.raises(BoundaryError):
        async for event in instance.stream(prepare_request("model", [message()], [tool()])):
            events.append(event)
    assert channel.closed and not instance._active
    assert not any(e.type in {"tool_call_start", "tool_call_end", "done"} for e in events)
