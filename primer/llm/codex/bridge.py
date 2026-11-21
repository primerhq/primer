"""Executor-free model step lifecycle.

The trusted factory is a code-level model transport, not an executable selected
by a user, a capability attestation, or a generic app-server RPC connection.
This foundation ships NO live factory. The executable offline harness drives
this exact class. Future live transport/auth/policy code requires separate
review before registration; passing these tests does not approve stock Codex.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Protocol

from primer.llm.codex.lifecycle import OwnedStream, drain
from primer.llm.codex.boundary import BoundaryError, BridgeConfig, PolicyAuthority, PolicySnapshot
from primer.llm.codex.request import ModelRequest, alias, compact, decode
from primer.model.chat import Done, ReasoningDelta, StreamEvent, TextDelta, ToolCallEnd, ToolCallStart, Usage


class ModelChannel(Protocol):
    def __aiter__(self) -> AsyncIterator[dict]: ...
    async def aclose(self) -> None: ...


# Trusted code: construction is synchronous, request I/O begins in __anext__.
# Cancellation during request startup is therefore inside bridge ownership.
TransportFactory = Callable[[dict], ModelChannel]


def _validate_request(request: ModelRequest, config: BridgeConfig) -> dict:
    if len(request.payload.encode()) > config.max_output_bytes:
        raise BoundaryError("Codex request exceeds size limit")
    try:
        wire = request.wire()
        allowed = {"model", "input", "tools", "store", "stream", "tool_choice", "text"}
        if type(wire) is not dict or set(wire) - allowed or wire.get("store") is not False or wire.get("stream") is not True:
            raise ValueError
        if type(wire["model"]) is not str or not wire["model"]:
            raise ValueError
        if type(wire["tools"]) is not list or len(wire["tools"]) > 1:
            raise ValueError
        aliases = dict(request.aliases)
        if len(aliases) != len(request.aliases) or len(set(aliases.values())) != len(aliases):
            raise ValueError
        if any(type(name) is not str or not name or key != alias(name) for key, name in request.aliases):
            raise ValueError
        declared = set()
        declarations = []
        if wire["tools"]:
            namespace = wire["tools"][0]
            if set(namespace) != {"type", "name", "tools"} or namespace["type"] != "namespace" or namespace["name"] != "primer":
                raise ValueError
            declarations = namespace["tools"]
        if type(declarations) is not list or len(declarations) > config.max_calls:
            raise ValueError
        for tool in declarations:
            if (set(tool) != {"type", "name", "description", "parameters"}
                    or tool["type"] != "function" or tool["name"] not in aliases
                    or tool["name"] in declared or type(tool["parameters"]) is not dict):
                raise ValueError
            declared.add(tool["name"])
        if declared != set(aliases):
            raise ValueError
        choice = wire.get("tool_choice", "auto")
        if type(choice) is str:
            if choice not in {"auto", "none", "required"} or (choice == "required" and not declared):
                raise ValueError
        elif type(choice) is dict:
            if set(choice) != {"type", "namespace", "name"} or choice["type"] != "function" or choice["namespace"] != "primer" or choice["name"] not in declared:
                raise ValueError
        else:
            raise ValueError
        if "text" in wire:
            text = wire["text"]
            fmt = text["format"]
            if (set(text) != {"format"} or set(fmt) != {"type", "name", "schema", "strict"}
                    or fmt["type"] != "json_schema" or fmt["name"] != "primer_output"
                    or fmt["strict"] is not True or type(fmt["schema"]) is not dict):
                raise ValueError
        if type(wire["input"]) is not list:
            raise ValueError
        calls, pending = set(), set()
        for item in wire["input"]:
            kind = item["type"]
            if kind == "message":
                if set(item) != {"type", "role", "content"} or item["role"] not in {"developer", "user", "assistant"}:
                    raise ValueError
                for part in item["content"]:
                    if set(part) != {"type", "text"} or part["type"] not in {"input_text", "output_text"} or type(part["text"]) is not str:
                        raise ValueError
            elif kind == "function_call":
                if set(item) != {"type", "namespace", "call_id", "name", "arguments"} or item["call_id"] in calls:
                    raise ValueError
                if item["namespace"] != "primer" or not isinstance(item["name"], str) or not item["name"].startswith("primer_") or type(decode(item["arguments"])) is not dict:
                    raise ValueError
                calls.add(item["call_id"])
                pending.add(item["call_id"])
            elif kind == "function_call_output":
                if set(item) != {"type", "call_id", "output"} or item["call_id"] not in pending or type(item["output"]) is not str:
                    raise ValueError
                pending.remove(item["call_id"])
            else:
                raise ValueError
        if pending:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BoundaryError("Unsupported Codex model request") from exc
    return wire


class _Translator:
    """Strict Responses subset; unknown native items never become tool calls.

    This is defense in depth. Native prevention comes from the absence of any
    executor/loader in ModelOnlyBridge and from admitting only function tools
    BEFORE the transport factory sees the request.
    """
    def __init__(self, request: ModelRequest, config: BridgeConfig):
        self.aliases = dict(request.aliases)
        self.choice = request.wire().get("tool_choice", "auto")
        self.config = config
        self.response_id: str | None = None
        self.complete = False
        self.bytes = 0
        self.calls: dict[str, dict] = {}
        self.item_ids: set[str] = set()
        self.indices: dict[str, int] = {}

    def accept(self, event: dict) -> list[StreamEvent]:
        try:
            return self._accept(event)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise BoundaryError("Malformed Codex model event") from exc

    def _accept(self, event: dict) -> list[StreamEvent]:
        size = len(compact(event).encode())
        self.bytes += size
        if size > self.config.max_frame_bytes or self.bytes > self.config.max_output_bytes:
            raise BoundaryError("Codex model output exceeds size limit")
        if self.complete:
            raise BoundaryError("Codex event follows terminal response")
        kind = event["type"]
        if kind == "response.created":
            if self.response_id is not None or type(event["response"]["id"]) is not str:
                raise BoundaryError("Duplicate or invalid Codex response")
            self.response_id = event["response"]["id"]
            return []
        if self.response_id is None:
            raise BoundaryError("Codex response was not started")
        if "response_id" in event and event["response_id"] != self.response_id:
            raise BoundaryError("Cross-response Codex event")
        if kind in {"response.output_text.delta", "response.reasoning_summary_text.delta"}:
            cls = TextDelta if kind == "response.output_text.delta" else ReasoningDelta
            return [cls(text=event["delta"], index=event["output_index"])]
        if kind in {"response.output_item.added", "response.output_item.done"}:
            item = event["item"]
            if item["type"] not in {"message", "reasoning", "function_call"}:
                raise BoundaryError("Native Codex output item refused")
            if kind.endswith("added"):
                if item["id"] in self.item_ids:
                    raise BoundaryError("Duplicate Codex item")
                self.item_ids.add(item["id"])
                index = event["output_index"]
                if type(index) is not int or index < 0 or index in self.indices.values():
                    raise BoundaryError("Invalid Codex output index")
                self.indices[item["id"]] = index
            elif item["id"] not in self.item_ids:
                raise BoundaryError("Unknown completed Codex item")
            if kind.endswith("done") and event["output_index"] != self.indices[item["id"]]:
                raise BoundaryError("Changed Codex output index")
            if item["type"] == "function_call" and kind.endswith("done"):
                if self.choice == "none" or (isinstance(self.choice, dict) and item["name"] != self.choice["name"]):
                    raise BoundaryError("Codex response violated tool choice")
                if item.get("namespace") != "primer" or item["name"] not in self.aliases or item["call_id"] in self.calls:
                    raise BoundaryError("Unknown or duplicate Primer tool call")
                arguments = decode(item["arguments"])
                if type(arguments) is not dict or len(self.calls) >= self.config.max_calls:
                    raise BoundaryError("Invalid Primer tool arguments or call limit")
                self.calls[item["call_id"]] = item
            return []
        if kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done",
                    "response.output_text.done", "response.reasoning_summary_text.done",
                    "response.content_part.added", "response.content_part.done",
                    "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
                    "response.in_progress"}:
            # Only terminal complete argument objects are offered to Primer.
            return []
        if kind != "response.completed":
            raise BoundaryError("Unsupported or failed Codex response")
        response = event["response"]
        if response["id"] != self.response_id or response["status"] != "completed":
            raise BoundaryError("Mismatched Codex terminal response")
        terminal_calls = {}
        for item in response["output"]:
            if item["type"] == "function_call":
                if item["call_id"] in terminal_calls:
                    raise BoundaryError("Duplicate terminal Primer tool call")
                terminal_calls[item["call_id"]] = item
            elif item["type"] not in {"message", "reasoning"}:
                raise BoundaryError("Native Codex terminal item refused")
        if terminal_calls != self.calls:
            raise BoundaryError("Incomplete or changed Codex tool calls")
        if not self.calls and self.choice != "auto" and self.choice != "none":
            raise BoundaryError("Codex response omitted required tool call")
        events: list[StreamEvent] = []
        for call_id, item in self.calls.items():
            index = self.indices[item["id"]]
            events.extend([ToolCallStart(id=call_id, name=self.aliases[item["name"]], index=index),
                           ToolCallEnd(id=call_id, arguments=decode(item["arguments"]), index=index)])
        usage = response.get("usage")
        if usage is not None:
            events.append(Usage(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                                cached_input_tokens=(usage.get("input_tokens_details") or {}).get("cached_tokens"),
                                reasoning_tokens=(usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
                                cumulative=False))
        events.append(Done(stop_reason="tool_use" if self.calls else "stop", raw_reason="completed"))
        self.complete = True
        return events


class ModelOnlyBridge:
    """Per-step channels, immutable admission, no history/resume/global RPC.

    It is not a sandbox against malicious Python dependencies. The authority
    and transport implementations are trusted code that must be reviewed and
    version-pinned before a live instance can be made. JSON callers cannot
    select either implementation or weaken the native ceiling.
    """
    def __init__(self, *, authority: PolicyAuthority, account: str,
                 transport_factory: TransportFactory, config: BridgeConfig | None = None):
        self.config = config or BridgeConfig()
        self._authority = authority
        self._account = account
        self._factory = transport_factory
        self._active: set[OwnedStream] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    async def _policy(self, model: str, expected: PolicySnapshot | None = None) -> PolicySnapshot:
        policy = await self._authority.current()
        policy.admit(self._account, model)
        if expected is not None and policy != expected:
            raise BoundaryError("Codex policy changed; start a fresh admitted step")
        return policy

    def stream(self, request: ModelRequest) -> OwnedStream:
        if self._closed:
            raise BoundaryError("Codex bridge is closed")
        if len(self._active) >= self.config.max_active_steps:
            raise BoundaryError("Codex bridge step capacity exceeded")
        stream = OwnedStream(self._stream(request), on_close=self._active.discard,
                             timeout=self.config.deadline_seconds)
        self._active.add(stream)
        return stream

    async def _stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        if self._closed or self._loop is not loop:
            raise BoundaryError("Codex bridge is closed or belongs to another event loop")
        channel = None
        terminal: list[StreamEvent] = []
        wire = _validate_request(request, self.config)
        try:
            policy = await self._policy(wire["model"])
            if self._closed:
                raise BoundaryError("Codex bridge closed during admission")
            # No await between admission and construction.
            channel = self._factory(wire)
            translator = _Translator(request, self.config)
            async for frame in channel:
                await self._policy(wire["model"], policy)
                if self._closed:
                    raise BoundaryError("Codex bridge closed before delivery")
                events = translator.accept(frame)
                if translator.complete:
                    terminal = events
                    break
                for event in events:
                    yield event
            if not translator.complete:
                raise BoundaryError("Codex stream ended without a terminal response")
        finally:
            if channel is not None:
                await self._close_channel(channel)
        # Every yield is a suspension point. Authorization linearizes per
        # event immediately before yielding, never once for an entire batch.
        # Primer still needs independent execution-time tool authorization.
        for event in terminal:
            async with asyncio.timeout(self.config.cleanup_seconds):
                await self._policy(wire["model"], policy)
            if self._closed:
                raise BoundaryError("Codex bridge closed before delivery")
            yield event

    async def _close_channel(self, channel: ModelChannel) -> None:
        async def close() -> None:
            async with asyncio.timeout(self.config.cleanup_seconds):
                await channel.aclose()
        cleanup = asyncio.create_task(close())
        await drain(cleanup)

    async def aclose(self) -> None:
        self._closed = True
        for stream in self._active:
            stream.ensure_can_close()
        # Close every retained stream, including idle generators whose last
        # anext consumer has already finished. Gather all cleanup attempts.
        results = await asyncio.gather(*(s.aclose() for s in tuple(self._active)), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
