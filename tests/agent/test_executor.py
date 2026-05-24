"""Tests for matrix.agent.executor.AgentExecutor."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

import pytest

from matrix.agent.compaction import CompactionStrategy
from matrix.agent.events import _ExecutorToolResult
from matrix.agent.executor import AgentExecutor
from matrix.agent.tool_manager import ToolExecutionManager
from matrix.model.agent import Agent, AgentModel
from matrix.model.chat import (
    Done,
    ExtendedEvent,
    Message,
    StreamEvent,
    StreamStart,
    TextDelta,
    TextPart,
    Tool,
    ToolCallEnd,
    ToolCallResult,
    ToolCallStart,
)
from matrix.model.common import Identifiable
from matrix.model.except_ import (
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from matrix.model.provider import LLMModel
from matrix.model.storage import (
    CursorPage,
    CursorPageResponse,
    FieldRef,
    OffsetPage,
    OffsetPageResponse,
    Op,
    PageRequest,
    Predicate,
    Value,
)
from matrix.model.thread import Thread, ThreadMessage


# ===========================================================================
# In-memory Storage[T] test double
# ===========================================================================


_T = TypeVar("_T", bound=Identifiable)


class _InMemoryStorage(Generic[_T]):
    """Minimal in-memory :class:`Storage` for tests."""

    def __init__(self, model_cls: type[_T]) -> None:
        self._model_cls = model_cls
        self._data: dict[str, _T] = {}

    async def get(self, id: str) -> _T | None:
        return self._data.get(id)

    async def create(self, entity: _T) -> _T:
        if entity.id in self._data:
            raise ConflictError(f"id {entity.id!r} already exists")
        self._data[entity.id] = entity
        return entity

    async def update(self, entity: _T) -> _T:
        if entity.id not in self._data:
            raise NotFoundError(f"no entity with id {entity.id!r}")
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page: PageRequest, *, order_by=None):
        return await self.find(None, page, order_by=order_by)

    async def find(
        self,
        predicate: Predicate | None,
        page: PageRequest,
        *,
        order_by=None,
    ):
        items = list(self._data.values())
        if predicate is not None:
            items = [i for i in items if self._eval(predicate, i)]
        if order_by:
            for ob in reversed(order_by):
                items.sort(
                    key=lambda x, f=ob.field: getattr(x, f),
                    reverse=(ob.direction == "desc"),
                )

        if isinstance(page, OffsetPage):
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        offset = int(page.cursor) if page.cursor else 0
        sliced = items[offset : offset + page.length]
        next_cursor: str | None = None
        if offset + page.length < len(items):
            next_cursor = str(offset + page.length)
        return CursorPageResponse(next_cursor=next_cursor, items=sliced)

    @staticmethod
    def _eval(p: Predicate, entity) -> bool:
        if p.op == Op.EQ:
            assert isinstance(p.left, FieldRef) and isinstance(p.right, Value)
            return getattr(entity, p.left.name) == p.right.value
        if p.op == Op.AND:
            assert isinstance(p.left, Predicate) and isinstance(p.right, Predicate)
            return _InMemoryStorage._eval(p.left, entity) and _InMemoryStorage._eval(
                p.right, entity
            )
        raise NotImplementedError(
            f"in-memory storage: op {p.op!r} not supported in tests"
        )


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeLLM:
    """Stub :class:`LLM` that returns scripted streams turn-by-turn."""

    def __init__(self, *, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self._cursor = 0
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["gpt-4o-mini"]

    def stream(
        self,
        *,
        model: str,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        idx = min(self._cursor, len(self._scripts) - 1)
        self._cursor += 1
        return self._stream_impl(self._scripts[idx])

    async def _stream_impl(
        self, events: list[StreamEvent]
    ) -> AsyncIterator[StreamEvent]:
        for ev in events:
            yield ev


class _FakeToolsetProvider:
    def __init__(self, *, toolset_id: str, tools: list[Tool], handler):
        self._toolset_id = toolset_id
        self._tools = tools
        self._handler = handler

    async def list_tools(
        self, *, principal: str | None = None
    ) -> AsyncIterator[Tool]:
        for t in self._tools:
            yield t

    async def call(
        self, *, tool_name: str, arguments, principal=None
    ) -> ToolCallResult:
        return await self._handler(tool_name, arguments, principal)


# ===========================================================================
# Helpers
# ===========================================================================


def _agent(
    *,
    system_prompt: list[str] | None = None,
    compaction_prompt: list[str] | None = None,
) -> Agent:
    return Agent(
        id="researcher",
        description="Research agent",
        model=AgentModel(provider_id="openai-1", model_name="gpt-4o-mini"),
        system_prompt=list(system_prompt or []),
        compaction_prompt=list(compaction_prompt or []),
    )


def _model(*, context_length: int = 128_000) -> LLMModel:
    return LLMModel(name="gpt-4o-mini", context_length=context_length)


def _tool(name: str = "echo") -> Tool:
    return Tool(
        id=name,
        description="echoes its input",
        toolset_id="t1",
        args_schema={"type": "object", "properties": {}, "additionalProperties": True},
    )


async def _build_executor(
    *,
    llm: _FakeLLM,
    handler=None,
    compaction: CompactionStrategy | None = None,
    system_prompt: list[str] | None = None,
    tools: list[Tool] | None = None,
) -> tuple[
    AgentExecutor,
    _InMemoryStorage[Thread],
    _InMemoryStorage[ThreadMessage],
    Thread,
]:
    if handler is None:

        async def _h(name, args, principal):
            return ToolCallResult(output=f"{name}({args})", is_error=False)

        handler = _h
    if tools is None:
        tools = [_tool()]

    agent = _agent(system_prompt=system_prompt)
    thread_storage: _InMemoryStorage[Thread] = _InMemoryStorage(Thread)
    message_storage: _InMemoryStorage[ThreadMessage] = _InMemoryStorage(ThreadMessage)
    thread = await AgentExecutor.open_thread(
        agent=agent,
        thread_storage=thread_storage,  # type: ignore[arg-type]
        title="t",
    )
    provider = _FakeToolsetProvider(toolset_id="t1", tools=tools, handler=handler)
    mgr = ToolExecutionManager(toolset_providers={"t1": provider})  # type: ignore[arg-type]
    executor = AgentExecutor(
        agent=agent,
        llm=llm,  # type: ignore[arg-type]
        llm_model=_model(),
        tool_manager=mgr,
        thread_id=thread.id,
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        compaction=compaction,
    )
    return executor, thread_storage, message_storage, thread


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


# ===========================================================================
# Thread management helpers
# ===========================================================================


class TestThreadManagement:
    @pytest.mark.asyncio
    async def test_open_thread(self) -> None:
        agent = _agent()
        ts: _InMemoryStorage[Thread] = _InMemoryStorage(Thread)
        thread = await AgentExecutor.open_thread(
            agent=agent, thread_storage=ts, title="hello"
        )
        assert thread.agent_id == "researcher"
        assert thread.title == "hello"
        loaded = await ts.get(thread.id)
        assert loaded == thread

    @pytest.mark.asyncio
    async def test_delete_thread_removes_messages(self) -> None:
        agent = _agent()
        ts: _InMemoryStorage[Thread] = _InMemoryStorage(Thread)
        ms: _InMemoryStorage[ThreadMessage] = _InMemoryStorage(ThreadMessage)
        thread = await AgentExecutor.open_thread(
            agent=agent, thread_storage=ts, title="x"
        )
        await ms.create(
            ThreadMessage(
                id="tmsg-1",
                thread_id=thread.id,
                role="user",
                parts=[TextPart(text="hi")],
                created_at=datetime.now(timezone.utc),
                sequence=0,
            )
        )
        await AgentExecutor.delete_thread(
            thread.id, thread_storage=ts, message_storage=ms
        )
        assert await ts.get(thread.id) is None
        assert await ms.get("tmsg-1") is None

    @pytest.mark.asyncio
    async def test_delete_thread_idempotent(self) -> None:
        ts: _InMemoryStorage[Thread] = _InMemoryStorage(Thread)
        ms: _InMemoryStorage[ThreadMessage] = _InMemoryStorage(ThreadMessage)
        await AgentExecutor.delete_thread(
            "no-such-thread", thread_storage=ts, message_storage=ms
        )

    @pytest.mark.asyncio
    async def test_list_threads_filters_by_agent(self) -> None:
        ts: _InMemoryStorage[Thread] = _InMemoryStorage(Thread)
        agent_a = Agent(
            id="a-1",
            description="x",
            model=AgentModel(provider_id="p", model_name="m"),
        )
        agent_b = Agent(
            id="b-1",
            description="x",
            model=AgentModel(provider_id="p", model_name="m"),
        )
        await AgentExecutor.open_thread(agent=agent_a, thread_storage=ts)
        await AgentExecutor.open_thread(agent=agent_a, thread_storage=ts)
        await AgentExecutor.open_thread(agent=agent_b, thread_storage=ts)
        page = await AgentExecutor.list_threads(
            thread_storage=ts,
            page=OffsetPage(length=10),
            agent_id="a-1",
        )
        assert page.length == 2  # type: ignore[union-attr]


# ===========================================================================
# Single-turn invoke
# ===========================================================================


class TestSingleTurn:
    @pytest.mark.asyncio
    async def test_simple_text_turn(self) -> None:
        llm = _FakeLLM(
            scripts=[
                [
                    StreamStart(model="gpt-4o-mini"),
                    TextDelta(text="hello", index=0),
                    TextDelta(text=" world", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        executor, ts, ms, thread = await _build_executor(llm=llm)
        events = await _drain(
            executor.invoke([Message(role="user", parts=[TextPart(text="hi")])])
        )
        assert any(isinstance(e, TextDelta) for e in events)
        assert any(isinstance(e, Done) for e in events)
        rows = sorted(ms._data.values(), key=lambda r: r.sequence)
        assert len(rows) == 2
        assert rows[0].role == "user"
        assert rows[1].role == "assistant"
        loaded_thread = await ts.get(thread.id)
        assert loaded_thread is not None
        assert loaded_thread.last_activity_at >= thread.last_activity_at

    @pytest.mark.asyncio
    async def test_tool_loop(self) -> None:
        llm = _FakeLLM(
            scripts=[
                [
                    StreamStart(model="gpt-4o-mini"),
                    # LLM emits the scoped tool id (toolset_id__bare_name);
                    # ToolExecutionManager splits and forwards the bare
                    # name to the provider's handler.
                    ToolCallStart(id="c-1", name="t1__echo", index=0),
                    ToolCallEnd(id="c-1", arguments={"v": "ping"}, index=0),
                    Done(stop_reason="tool_use", raw_reason="tool_use"),
                ],
                [
                    StreamStart(model="gpt-4o-mini"),
                    TextDelta(text="echoed: ping", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ],
            ]
        )

        async def _handler(name, args, principal):
            assert name == "echo"
            return ToolCallResult(output=f"echo:{args['v']}", is_error=False)

        executor, _, ms, _ = await _build_executor(llm=llm, handler=_handler)
        events = await _drain(
            executor.invoke([Message(role="user", parts=[TextPart(text="run echo")])])
        )
        synth_events = [
            e
            for e in events
            if isinstance(e, ExtendedEvent)
            and isinstance(e.extended, _ExecutorToolResult)
        ]
        assert len(synth_events) == 1
        assert synth_events[0].extended.output == "echo:ping"

        rows = sorted(ms._data.values(), key=lambda r: r.sequence)
        assert [r.role for r in rows] == ["user", "assistant", "tool", "assistant"]


# ===========================================================================
# History loading on second invoke
# ===========================================================================


class TestHistoryLoading:
    @pytest.mark.asyncio
    async def test_second_invoke_includes_prior_messages(self) -> None:
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="A", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ],
                [
                    TextDelta(text="B", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ],
            ]
        )
        executor, _, _, _ = await _build_executor(llm=llm)
        await _drain(
            executor.invoke([Message(role="user", parts=[TextPart(text="hi")])])
        )
        await _drain(
            executor.invoke([Message(role="user", parts=[TextPart(text="more")])])
        )
        second_call_msgs = llm.calls[1]["messages"]
        roles = [m.role for m in second_call_msgs]
        assert roles == ["user", "assistant", "user"]


# ===========================================================================
# Streaming taps
# ===========================================================================


class TestStreamingTaps:
    @pytest.mark.asyncio
    async def test_subscriber_receives_events(self) -> None:
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="hi", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        executor, _, _, _ = await _build_executor(llm=llm)

        seen: list[StreamEvent] = []

        class _Cap:
            async def on_event(self, ev: StreamEvent) -> None:
                seen.append(ev)

        sub = executor.subscribe(_Cap())
        await _drain(
            executor.invoke([Message(role="user", parts=[TextPart(text="x")])])
        )
        assert any(isinstance(e, TextDelta) for e in seen)
        await sub.unsubscribe()

    @pytest.mark.asyncio
    async def test_failing_subscriber_isolated(self) -> None:
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="hi", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        executor, _, _, _ = await _build_executor(llm=llm)

        ok_seen: list[StreamEvent] = []

        class _Boom:
            async def on_event(self, ev: StreamEvent) -> None:
                raise RuntimeError("subscriber failure")

        class _Ok:
            async def on_event(self, ev: StreamEvent) -> None:
                ok_seen.append(ev)

        executor.subscribe(_Boom())
        executor.subscribe(_Ok())
        await _drain(
            executor.invoke([Message(role="user", parts=[TextPart(text="x")])])
        )
        assert any(isinstance(e, TextDelta) for e in ok_seen)


# ===========================================================================
# Hard-overflow recovery
# ===========================================================================


class TestHardOverflow:
    @pytest.mark.asyncio
    async def test_overflow_triggers_force_compact_and_retry(self) -> None:
        class _OverflowingLLM(_FakeLLM):
            def __init__(self) -> None:
                super().__init__(
                    scripts=[
                        # compaction summarisation call
                        [
                            TextDelta(text="summary", index=0),
                            Done(stop_reason="stop", raw_reason="stop"),
                        ],
                        # retried turn
                        [
                            TextDelta(text="OK", index=0),
                            Done(stop_reason="stop", raw_reason="stop"),
                        ],
                    ]
                )
                self._overflowed = False

            def stream(self, *, model, messages, **kwargs):
                if not self._overflowed:
                    self._overflowed = True
                    self.calls.append(
                        {"model": model, "messages": list(messages), **kwargs}
                    )
                    return self._raise_iter()
                return super().stream(model=model, messages=messages, **kwargs)

            async def _raise_iter(self):
                raise BadRequestError("input is too long for context length")
                if False:  # pragma: no cover
                    yield

        llm = _OverflowingLLM()
        # Use a tighter tail_turns so force_compact's _full_compact has a
        # non-empty head and actually calls the LLM (consuming script[0]).
        compaction = CompactionStrategy(tail_turns=1)
        executor, _, ms, thread = await _build_executor(
            llm=llm, compaction=compaction
        )
        big = "prior content " * 200
        for i in range(10):  # 5 assistants -> head non-empty with tail_turns=1
            role = "user" if i % 2 == 0 else "assistant"
            await ms.create(
                ThreadMessage(
                    id=f"tmsg-pre-{i}",
                    thread_id=thread.id,
                    role=role,
                    parts=[TextPart(text=big)],
                    created_at=datetime.now(timezone.utc),
                    sequence=i,
                )
            )

        events = await _drain(
            executor.invoke([Message(role="user", parts=[TextPart(text="now")])])
        )
        assert any(isinstance(e, TextDelta) and "OK" in e.text for e in events)
        assert len(llm.calls) >= 3


# ===========================================================================
# Compaction integration (proactive path)
# ===========================================================================


class TestCompactionIntegration:
    @pytest.mark.asyncio
    async def test_proactive_compaction_rewrites_history(self) -> None:
        llm = _FakeLLM(
            scripts=[
                # 1) compaction summarisation call
                [
                    TextDelta(text="brief summary", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ],
                # 2) the actual user turn after compaction
                [
                    TextDelta(text="answered", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ],
            ]
        )
        compaction = CompactionStrategy(
            trigger_ratio=0.01,  # always triggers given enough data
            reserved_output_tokens=0,
            tail_turns=2,
            prune_per_output_tokens=1_000_000,
            prune_total_threshold=1_000_000,
        )
        executor, _, ms, thread = await _build_executor(llm=llm, compaction=compaction)
        # Pre-seed enough text that 0.01 * 128k trigger is exceeded
        # (~1280 tokens). Each message is ~750 tokens after estimation,
        # so 6 messages comfortably trips the trigger.
        big = "prior content " * 200
        for i in range(6):
            role = "user" if i % 2 == 0 else "assistant"
            await ms.create(
                ThreadMessage(
                    id=f"tmsg-pre-{i}",
                    thread_id=thread.id,
                    role=role,
                    parts=[TextPart(text=big)],
                    created_at=datetime.now(timezone.utc),
                    sequence=i,
                )
            )

        await _drain(
            executor.invoke([Message(role="user", parts=[TextPart(text="now")])])
        )
        rows = sorted(ms._data.values(), key=lambda r: r.sequence)
        # First persisted row after compaction should be the assistant-role
        # summary message (compaction inserted it at sequence 0).
        assert rows[0].role == "assistant"
        assert "brief summary" in rows[0].parts[0].text or "earlier conversation compacted" in rows[0].parts[0].text  # type: ignore[union-attr]
