"""The local MCP tool bridge: lists a step's tools and never answers a call."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from primer.llm.claude_code.bridge import ToolBridge, aclose_bridge, alias_for, get_bridge
from primer.model.chat import Tool


def test_alias_short_ids_pass_through():
    assert alias_for("system__list_agents") == "system__list_agents"


def test_alias_long_ids_are_hashed_and_distinct():
    a, b = "x" * 50 + "__one_tool", "x" * 50 + "__two_tool"
    assert len(alias_for(a)) == 56 and alias_for(a) != alias_for(b)


def _tool(scoped_id: str) -> Tool:
    return Tool(
        id=scoped_id,
        description=f"the {scoped_id} tool",
        toolset_id="system",
        args_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )


@pytest.fixture
async def bridge():
    b = ToolBridge()
    try:
        yield b
    finally:
        await b.aclose()


def _origin(url: str) -> str:
    return url[: url.index("/", 8)]


async def test_tools_list_returns_step_tools(bridge: ToolBridge):
    tool = _tool("system__list_agents")
    step = await bridge.register([tool])
    try:
        async with streamable_http_client(url=step.url) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 5)
                listed = (await asyncio.wait_for(session.list_tools(), 5)).tools
        assert [t.name for t in listed] == ["system__list_agents"]
        assert listed[0].input_schema == tool.args_schema
    finally:
        await step.close()


async def test_unknown_token_404(bridge: ToolBridge):
    step = await bridge.register([_tool("system__list_agents")])
    try:
        async with httpx.AsyncClient() as client:
            r = await asyncio.wait_for(
                client.post(_origin(step.url) + "/nope/mcp", json={"method": "x"}), 5
            )
        assert r.status_code == 404
    finally:
        await step.close()


async def test_tools_call_is_never_answered(bridge: ToolBridge):
    step = await bridge.register([_tool("system__list_agents")])
    try:
        async with streamable_http_client(url=step.url) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 5)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(session.call_tool("system__list_agents", {}), 1)
    finally:
        await step.close()


async def test_step_close_unregisters(bridge: ToolBridge):
    step = await bridge.register([_tool("system__list_agents")])
    await step.close()
    async with httpx.AsyncClient() as client:
        r = await asyncio.wait_for(client.post(step.url, json={}), 5)
    assert r.status_code == 404


async def test_long_ids_map_back_to_their_own_tool(bridge: ToolBridge):
    a, b = "x" * 50 + "__one_tool", "x" * 50 + "__two_tool"
    step = await bridge.register([_tool(a), _tool(b)])
    try:
        assert step.aliases[alias_for(a)] == a
        assert step.aliases[alias_for(b)] == b
        async with streamable_http_client(url=step.url) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 5)
                listed = (await asyncio.wait_for(session.list_tools(), 5)).tools
        assert [t.name for t in listed] == [alias_for(a), alias_for(b)]
    finally:
        await step.close()


async def test_get_bridge_is_lazy_singleton():
    b1 = await get_bridge()
    b2 = await get_bridge()
    assert b1 is b2
    step = await b1.register([_tool("system__list_agents")])
    await step.close()
    await b1.aclose()


# --- Fix round 1: a step's lifecycle is its own, and held calls stay held ---

def test_held_call_stays_held_in_two_separate_loops():
    """The never-answered handler must not wait on a loop-bound (e.g. module-level)
    Event: a held call must still be held after 1 s in a SECOND event loop. With a
    module-level Event the second loop's wait() raises and the call errors instead
    of staying held."""

    async def scenario() -> bool:
        b = ToolBridge()
        step = await b.register([_tool("system__list_agents")])
        held = False
        async with streamable_http_client(url=step.url) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 5)
                try:
                    await asyncio.wait_for(session.call_tool("system__list_agents", {}), 1)
                    held = False  # answered within 1 s: not held
                except TimeoutError:
                    held = True   # still pending after 1 s
        await b.aclose()
        return held

    assert asyncio.run(scenario())
    assert asyncio.run(scenario())


async def test_close_step_from_a_different_task(bridge: ToolBridge):
    """close() must be awaitable from any task: the old code exited the session
    manager's cancel scope (entered by register) from whatever task called close,
    which raised 'Attempted to exit cancel scope in a different task'."""
    step = await bridge.register([_tool("system__list_agents")])
    closer = asyncio.get_running_loop().create_task(step.close())
    await asyncio.wait_for(closer, 2)
    async with httpx.AsyncClient() as client:
        r = await asyncio.wait_for(client.post(step.url, json={}), 5)
    assert r.status_code == 404


async def test_aclose_with_a_held_call_still_connected(bridge: ToolBridge):
    """aclose() must complete (within 2 s) while a client's tool call is still
    held, so registry shutdown / Stop finalisation never hang on a pending call."""
    step = await bridge.register([_tool("system__list_agents")])
    call_sent = asyncio.Event()

    async def client_holds_a_call():
        async with streamable_http_client(url=step.url) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 5)
                call_sent.set()
                await session.call_tool("system__list_agents", {})  # held by the bridge

    client = asyncio.create_task(client_holds_a_call())
    await asyncio.wait_for(call_sent.wait(), 5)
    # Give the call time to reach the bridge and be held before the close, so the close
    # really races a held request instead of one that was never sent.
    await asyncio.sleep(0.3)
    await asyncio.wait_for(bridge.aclose(), 2)
    client.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await client


async def test_close_of_a_held_call_logs_no_mcp_error(bridge: ToolBridge, caplog):
    """Ending a held call at teardown must not log an ERROR: mcp 2.3.0 logs a handler that
    raises through logger.exception (mcp/shared/jsonrpc_dispatcher.py), so a step close with
    a held call would leave an ERROR record per held call."""
    step = await bridge.register([_tool("system__list_agents")])
    call_sent = asyncio.Event()

    async def client_holds_a_call():
        async with streamable_http_client(url=step.url) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 5)
                call_sent.set()
                await session.call_tool("system__list_agents", {})  # held by the bridge

    client = asyncio.create_task(client_holds_a_call())
    await asyncio.wait_for(call_sent.wait(), 5)
    await asyncio.sleep(0.3)
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(bridge.aclose(), 2)
    client.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await client
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith("mcp")]
    assert not errors, [r.getMessage() for r in errors]


# --- Teardown: a dead bridge or a cancelled serve task never raises into the caller ---

def test_aclose_bridge_from_a_dead_loop_resets_the_singleton():
    """A bridge started on a loop that has since ended (an earlier test, or an
    earlier asyncio.run) must not be awaited on a later loop: the loop's
    teardown cancelled its serve and step tasks, and awaiting any of them would
    raise that cancellation into the caller (the app's lifespan shutdown in CI).
    aclose_bridge closes the stale listening socket synchronously and resets the
    globals, and a later get_bridge starts a fresh, working bridge."""

    async def start() -> None:
        b = await get_bridge()
        await b.register([_tool("system__list_agents")])  # so it actually serves
        # the loop ends with the bridge live: its teardown cancels both tasks

    asyncio.run(start())

    async def close_and_reget() -> None:
        await aclose_bridge()  # must complete without raising
        fresh = await get_bridge()
        step = await fresh.register([_tool("system__list_agents")])
        assert step.url.startswith("http://127.0.0.1:")
        await step.close()
        await fresh.aclose()

    asyncio.run(close_and_reget())


async def test_aclose_never_raises_a_cancelled_task_into_the_caller():
    """A serve task or a step run task cancelled by something we did not do (the
    loop's own teardown does this) must not raise its CancelledError out of
    aclose(): aclose waits for the task's outcome without raising it, and the
    socket is closed either way."""
    bridge = ToolBridge()
    step = await bridge.register([_tool("system__list_agents")])
    assert step.url.startswith("http://127.0.0.1:")
    bridge._task.cancel()  # the loop's teardown would do this
    step_task = next(iter(bridge._steps.values()))._task
    step_task.cancel()
    await bridge.aclose()  # returns normally, never raises the tasks' cancellation
    assert bridge._sock is None


async def test_cancelling_the_task_awaiting_aclose_still_raises():
    """Our OWN cancellation of the task awaiting aclose() must still propagate:
    keeping a dead task's outcome from raising must not swallow the caller's
    cancel. The stub serve task never honours should_exit, so aclose is really
    waiting on it when we cancel."""
    import types

    bridge = ToolBridge()
    bridge._uvicorn = types.SimpleNamespace(should_exit=False)

    async def _serve_forever() -> None:
        await asyncio.Event().wait()

    serve = asyncio.create_task(_serve_forever())
    bridge._task = serve
    closer = asyncio.create_task(bridge.aclose())
    await asyncio.sleep(0.05)  # aclose is now awaiting the serve task
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    serve.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serve


async def test_aclose_cancelled_by_its_caller_closes_the_socket_cleanly(caplog):
    """A real bridge: the caller is cancelled while aclose() waits, so the serve
    task keeps running. The finally must stop the asyncio servers (the loop's
    selector drops the fd) BEFORE closing the raw socket, and the serve task's
    outcome must be read: otherwise the serve task's own shutdown hits the dead
    fd (a ValueError out of a task nobody holds, reported through the loop's
    exception handler, a "Task exception was never retrieved" log, and a stale
    selector entry for the fd)."""
    import gc
    import logging

    bridge = ToolBridge()
    step = await bridge.register([_tool("system__list_agents")])
    await step.close()
    fd = bridge._sock.fileno()
    recorded = []
    loop = asyncio.get_running_loop()

    def _record(context):
        exc = context.get("exception")
        if exc is not None:
            recorded.append(exc)

    loop.set_exception_handler(_record)
    with caplog.at_level(logging.WARNING):
        try:
            closer = asyncio.create_task(bridge.aclose())
            await asyncio.sleep(0)  # let aclose reach its await of the serve task
            closer.cancel()  # the caller is cancelled while aclose waits
            with pytest.raises(asyncio.CancelledError):
                await closer
            await asyncio.sleep(0.5)  # let the serve task wake, shut down, and log anything
            assert bridge._sock is None
            gc.collect()
        finally:
            loop.set_exception_handler(None)
    dangling = [r for r in caplog.records if "exception was never retrieved" in r.getMessage()]
    assert not recorded, [repr(e) for e in recorded]
    assert not dangling, [r.getMessage() for r in dangling]
    try:
        loop._selector.get_key(fd)
    except KeyError:
        pass  # the fd is gone from the selector
    else:
        pytest.fail(f"the selector still holds a stale entry for fd {fd}")


async def test_aclose_cancels_the_serve_task_on_timeout(monkeypatch):
    """The old wait_for cancelled the serve task when the wait timed out; the
    asyncio.wait rewrite keeps that, so an uncooperative server does not
    outlive the close (guard for the timeout path)."""
    import types

    monkeypatch.setattr("primer.llm.claude_code.bridge._SERVE_STOP_S", 0.1)
    bridge = ToolBridge()
    bridge._uvicorn = types.SimpleNamespace(should_exit=False)

    async def _serve_forever() -> None:
        await asyncio.Event().wait()

    serve = asyncio.create_task(_serve_forever())
    bridge._task = serve
    await bridge.aclose()
    for _ in range(50):
        if serve.cancelled():
            break
        await asyncio.sleep(0.01)
    assert serve.cancelled(), "the serve task must be cancelled when the wait times out"
    assert bridge._sock is None
