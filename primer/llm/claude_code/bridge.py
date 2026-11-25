"""The local MCP tool bridge for agent runtimes.

Provider-neutral: a future Codex runtime reuses this module, so nothing here
imports or mentions Claude in particular.

Every agent-runtime step runs the CLI with an ``--mcp-config`` pointing its MCP
client at a per-step path on THIS bridge. The bridge lists exactly the primer
tools of the current step (under their alias names, with their own JSON
schemas) and NEVER answers a call: when the CLI asks to run a tool, the step's
call handler awaits the step's ``closed`` event, so the request stays pending.
Primer reads the call from the CLI's output stream and kills the CLI process;
closing the step sets ``closed`` (the CLI is already dead, so the handler may
error) and stops the step's run task, which cancels the pending handler.

One process serves one uvicorn on a loopback ephemeral port; each step gets its
own path token, its own lowlevel ``Server`` (MCP server name ``p``, so the
model sees ``mcp__p__<alias>``) and its own ``StreamableHTTPSessionManager``
(stateless, JSON responses), so steps never see each other's tools.

Each step's session manager runs in a bridge-owned task (``async with
manager.run(): ...``), so the manager's cancel scope is entered and exited in
the same task and ``close`` can be awaited from any task.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    Tool as McpTool,
)

from primer.llm.agent_runtime.alias import tool_alias
from primer.model.chat import Tool

#: Alias length bound (global constraints): scoped ids of at most 56 chars pass
#: through unchanged; longer ones become 47 chars + "_" + 8 hex of the full
#: id's SHA-256 (47 + 1 + 8 == 56).
_ALIAS_MAX = 56
_ALIAS_HEAD = 47

#: How long ``aclose`` waits for the uvicorn task to honour ``should_exit``.
_SERVE_STOP_S = 5.0


class _NoSignalServer(uvicorn.Server):
    """A uvicorn server that leaves the process's signal handlers alone.

    ``uvicorn.Server.serve()`` runs under ``capture_signals()``, which installs
    SIGINT/SIGTERM handlers for the whole process; running the bridge inside
    primer would swallow primer's own shutdown signals, so make it a no-op.
    """

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def alias_for(scoped_id: str) -> str:
    """The alias a scoped tool id is listed under on the bridge.

    The shared agent-runtime rule (``primer.llm.agent_runtime.alias.tool_alias``)
    with the bridge's limits: short ids (``_ALIAS_MAX`` chars or fewer) pass
    through unchanged; longer ones become their first ``_ALIAS_HEAD`` chars, an
    underscore, and the first 8 hex chars of the SHA-256 of the FULL id, so two
    ids sharing a long prefix still get distinct aliases that map back to
    their own id.
    """
    return tool_alias(scoped_id, max_len=_ALIAS_MAX, keep_len=_ALIAS_HEAD)


async def _send_404(send: Any) -> None:
    body = b"not found"
    await send({
        "type": "http.response.start",
        "status": 404,
        "headers": [
            (b"content-type", b"text/plain"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _dispatch_app(steps: dict[str, "_Step"]) -> "Callable[[dict[str, Any], Any, Any], Awaitable[None]]":
    """The raw ASGI app: route ``/{token}/mcp`` (or ``/{token}/mcp/``) to that
    step's session manager; answer 404 for everything else, including unknown
    tokens. The closure reads the shared steps dict, so registering a step is
    visible to it without rebuilding the app. A plain function (not a bound
    method): uvicorn treats those as ASGI3, bound methods as ASGI2."""

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        parts = scope["path"].strip("/").split("/")
        step = steps.get(parts[0]) if len(parts) == 2 and parts[1] == "mcp" else None
        if step is None:
            await _send_404(send)
            return
        await step.manager.handle_request(scope, receive, send)

    return app


@dataclass
class BridgeStep:
    """One step's bridge path: its URL and the alias -> scoped id mapping.

    ``aliases`` maps each alias the step's tools are listed under back to the
    scoped tool id, so primer can route a call the CLI made on the alias.
    """

    url: str
    aliases: dict[str, str]
    _bridge: "ToolBridge | None" = field(repr=False, default=None)
    _token: str = field(repr=False, default="")

    async def close(self) -> None:
        """Deregister this step: its path 404s and its pending calls cancel."""
        if self._bridge is not None and self._token:
            await self._bridge._deregister(self._token)


class _Step:
    """Bridge-side state of one registered step.

    Its session manager runs in ``self._task`` (a bridge-owned task), so the
    manager's cancel scope is entered and exited in that SAME task; ``close``
    can therefore be awaited from any task. ``closed`` is what the never-answered
    call handler awaits; ``stop`` tells the run task to exit ``manager.run()``
    (which cancels the pending handler); ``started`` is set once the manager is
    live.
    """

    def __init__(
        self,
        token: str,
        manager: StreamableHTTPSessionManager,
        closed: asyncio.Event,
        stop: asyncio.Event,
        started: asyncio.Event,
    ) -> None:
        self.token = token
        self.manager = manager
        self.closed = closed
        self._stop = stop
        self._started = started
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        # Enter and exit manager.run() in this (bridge-owned) task only.
        async with self.manager.run():
            self._started.set()
            await self._stop.wait()

    async def close(self) -> None:
        # Wake the never-answered call handler (primer has already killed the
        # CLI by then, so erroring is safe) and stop the run task. The run task
        # is bridge-owned and we never cancel it, so wait for its outcome with
        # asyncio.wait, which never raises the task's own outcome: a
        # cancellation or crash of the task (the loop's teardown does this)
        # cannot raise into the caller of close.
        self.closed.set()
        self._stop.set()
        if self._task is not None:
            task = self._task
            await asyncio.wait({task}, timeout=_SERVE_STOP_S)
            if task.done() and not task.cancelled():
                task.exception()  # retrieve: no "exception was never retrieved"


class ToolBridge:
    """A loopback MCP server holding one stateless session per registered step."""

    def __init__(self) -> None:
        self._steps: dict[str, _Step] = {}
        self._app = _dispatch_app(self._steps)
        self._uvicorn: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._sock: socket.socket | None = None

    async def _ensure_serving(self) -> None:
        """Start (or restart) the bridge's uvicorn on a pre-bound loopback socket."""
        if self._task is not None and not self._task.done():
            return  # already serving
        # First start, or a previous serve task crashed: (re)bind a fresh socket.
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        self._sock = sock
        # log_config=None / log_level=None keep uvicorn from re-running
        # dictConfig over the process's logging.
        self._uvicorn = _NoSignalServer(
            uvicorn.Config(self._app, log_config=None, log_level=None, lifespan="off")
        )
        self._task = asyncio.get_running_loop().create_task(self._uvicorn.serve(sockets=[sock]))

    async def register(self, tools: Sequence[Tool]) -> BridgeStep:
        """Serve exactly ``tools`` (under their alias names) at a fresh per-step URL.

        The step's path token is random; unknown tokens get 404, and the step's
        ``tools/call`` requests are never answered (see the module docstring).
        """
        await self._ensure_serving()
        token = secrets.token_urlsafe(16)
        tools_by_alias = {alias_for(t.id): t for t in tools}

        # Per-step control events, created in the running loop. Never a
        # module-level event: those bind to the first loop and raise on a later
        # one. ``closed`` is what the never-answered handler waits on.
        closed = asyncio.Event()
        stop = asyncio.Event()
        started = asyncio.Event()

        async def _list(ctx: Any, params: PaginatedRequestParams | None) -> ListToolsResult:
            return ListToolsResult(
                tools=[
                    McpTool(name=alias, description=tool.description, input_schema=tool.args_schema)
                    for alias, tool in tools_by_alias.items()
                ]
            )

        async def _call(ctx: Any, params: CallToolRequestParams) -> CallToolResult:
            # Never answered while the step is live: primer kills the CLI process
            # before the step closes; ``closed`` is set only on teardown, by which
            # time the CLI is dead. Return an error result instead of raising: mcp
            # 2.3.0 logs a raising handler through logger.exception
            # (mcp/shared/jsonrpc_dispatcher.py), which would leave an ERROR record
            # per held call on every close.
            await closed.wait()
            return CallToolResult(isError=True, content=[])

        server = Server("p", on_list_tools=_list, on_call_tool=_call)
        manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)
        step = _Step(token, manager, closed, stop, started)
        step._task = asyncio.get_running_loop().create_task(step._run())
        await started.wait()  # the manager is live before the URL is handed out
        self._steps[token] = step
        port = self._sock.getsockname()[1]
        return BridgeStep(
            url=f"http://127.0.0.1:{port}/{token}/mcp",
            aliases={alias: t.id for alias, t in tools_by_alias.items()},
            _bridge=self,
            _token=token,
        )

    async def _deregister(self, token: str) -> None:
        step = self._steps.pop(token, None)
        if step is not None:
            await step.close()

    async def aclose(self) -> None:
        """Close every step, then stop the bridge's own server."""
        for token in list(self._steps):
            await self._deregister(token)
        uv, self._uvicorn = self._uvicorn, None
        task, self._task = self._task, None
        if uv is not None:
            uv.should_exit = True
        try:
            if task is not None:
                # asyncio.wait never raises the task's own outcome, so a serve
                # task cancelled by something we did not do (the loop's teardown
                # does this) cannot raise its CancelledError into the caller
                # (the app's lifespan shutdown); our own cancellation still
                # propagates. The done callback reads the outcome either way,
                # so a task nobody else holds cannot log "exception was never
                # retrieved".
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
                _, pending = await asyncio.wait({task}, timeout=_SERVE_STOP_S)
                if pending:
                    # The old wait_for cancelled the task on timeout: keep
                    # that, so an uncooperative server does not outlive the
                    # close.
                    task.cancel()
        finally:
            # Close the socket even if the serve task crashed above. While the
            # serve task is still running, stop the asyncio servers first, so
            # the loop's selector drops the fd BEFORE the raw socket is
            # closed: closing it first leaves a stale reader, and the serve
            # task's own shutdown then hits the dead fd (a ValueError out of a
            # task nobody holds).
            if task is not None and not task.done():
                for server in getattr(uv, "servers", []):
                    server.close()
            if self._sock is not None:
                self._sock.close()
                self._sock = None


_bridge: ToolBridge | None = None
_bridge_loop: asyncio.AbstractEventLoop | None = None


def _drop_stale_bridge(bridge: ToolBridge) -> None:
    """Drop a bridge whose loop ended or whose serve task died, without
    awaiting anything of it: close the listening socket synchronously, and
    read every dropped task's outcome (a done callback that never raises), so
    nothing leaks and no crash can log "exception was never retrieved"."""
    sock = bridge._sock
    if sock is not None and sock.fileno() != -1:
        sock.close()
    bridge._sock = None
    for task in (bridge._task, *(step._task for step in bridge._steps.values())):
        if task is None:
            continue
        if task.done():
            task.cancelled() or task.exception()  # read the outcome without raising
        else:
            task.add_done_callback(lambda t: t.cancelled() or t.exception())


async def get_bridge() -> ToolBridge:
    """The process-wide bridge (lazy), bound to the running event loop.

    A bridge is only valid on the loop that started it: if the running loop
    differs, or the bridge's serve task has died, a fresh bridge is built. A
    replaced bridge is dropped without being awaited, but its socket is closed
    and its tasks' outcomes read (see :func:`_drop_stale_bridge`).
    """
    global _bridge, _bridge_loop
    loop = asyncio.get_running_loop()
    if (
        _bridge is None
        or _bridge_loop is not loop
        or (_bridge._task is not None and _bridge._task.done())
    ):
        if _bridge is not None:
            _drop_stale_bridge(_bridge)
        _bridge = ToolBridge()
        _bridge_loop = loop
    return _bridge


async def aclose_bridge() -> None:
    """Close the process-wide bridge (the provider registry's teardown hook).

    A no-op when no bridge was started yet. A bridge started on a loop that has
    since ended is never awaited (its tasks were cancelled by that loop's
    teardown, and awaiting them would raise their cancellation into the
    caller): its listening socket is closed synchronously instead. Resets the
    globals so a later :func:`get_bridge` on a live loop starts a fresh bridge
    rather than reusing a closed one.
    """
    global _bridge, _bridge_loop
    if _bridge is not None:
        try:
            if _bridge_loop is asyncio.get_running_loop():
                await _bridge.aclose()
            else:
                # A bridge started on a loop that has since ended: awaiting
                # any of its tasks would raise their cancellation into the
                # caller; drop it synchronously instead.
                _drop_stale_bridge(_bridge)
        finally:
            _bridge, _bridge_loop = None, None
