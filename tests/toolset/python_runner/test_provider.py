"""The provider: ToolsetProvider contract over the python runner."""

from __future__ import annotations

from typing import Any

import pytest

from primer.model.providers.toolset import PythonConfig
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset.python_runner.provider import (
    PythonToolsetProvider,
    python_tool_resume,
    scoped_tool_name,
)
from primer.toolset.python_runner.protocol import ShimResponse
from primer.toolset.python_runner.runners import (
    IsolationLevel,
    LocalHardenedRunner,
    Runner,
)
from primer.worker.yield_resume_registry import ResumeContext, get_resume_hook

SRC = '''
@primer_tool()
def greet(name: str) -> str:
    """Greet a person by name.

    Use when you need a friendly greeting.

    Args:
        name: Who to greet.
    """
    return "hello " + name


@primer_tool()
def ask(question: str, ctx) -> str:
    """Ask the operator something.

    Use when a human must decide.

    Args:
        question: What to ask.
    """
    return ask_user(question)


@resumes(ask)
def _ask_resume(payload: dict, meta: dict) -> str:
    """Return the answer.

    Use when resuming.

    Args:
        payload: The response payload.
        meta: The resume metadata.
    """
    return payload["response"]
'''

CTX = ToolContext(tool_call_id="tc-1", session_id="s-1", workspace_id=None)


def _provider(
    src: str = SRC,
    version: int = 2,
    runner=None,
    sandbox_runner_for=None,
) -> PythonToolsetProvider:
    return PythonToolsetProvider(
        toolset_id="ts-1",
        config=PythonConfig(source=src, source_version=version),
        runner=runner or LocalHardenedRunner(),
        sandbox_runner_for=sandbox_runner_for,
    )


@pytest.mark.asyncio
async def test_list_tools_yields_every_registered_tool() -> None:
    assert {t.id async for t in _provider().list_tools()} == {"greet", "ask"}


@pytest.mark.asyncio
async def test_every_tool_carries_the_provider_toolset_id() -> None:
    async for t in _provider().list_tools():
        assert t.toolset_id == "ts-1"


@pytest.mark.asyncio
async def test_calling_a_tool_runs_the_function() -> None:
    res = await _provider().call(tool_name="greet", arguments={"name": "ada"})
    assert res.is_error is False
    assert res.output == "hello ada"


@pytest.mark.asyncio
async def test_a_raising_tool_is_an_error_result_not_an_exception() -> None:
    src = (
        "@primer_tool()\n"
        "def boom(a: str) -> str:\n"
        '    """Explode.\n\n    Use when testing.\n\n    Args:\n        a: A.\n    """\n'
        "    raise ValueError('nope')\n"
    )
    res = await _provider(src).call(tool_name="boom", arguments={"a": "1"})
    assert res.is_error is True
    assert "nope" in res.output
    assert res.extended["traceback"]


@pytest.mark.asyncio
async def test_an_unknown_tool_is_an_error_result() -> None:
    res = await _provider().call(tool_name="nope", arguments={})
    assert res.is_error is True


@pytest.mark.asyncio
async def test_a_timeout_is_an_error_result_not_a_hang() -> None:
    src = (
        "@primer_tool(timeout_seconds=1)\n"
        "def spin(a: str) -> str:\n"
        '    """Spin.\n\n    Use when testing.\n\n    Args:\n        a: A.\n    """\n'
        "    while True:\n"
        "        pass\n"
    )
    res = await _provider(src).call(tool_name="spin", arguments={"a": "1"})
    assert res.is_error is True
    assert "timeout" in res.output.lower()


@pytest.mark.asyncio
async def test_a_yielding_tool_raises_yield_to_worker() -> None:
    with pytest.raises(YieldToWorker) as exc:
        await _provider().call(
            tool_name="ask", arguments={"question": "ship it?"}, ctx=CTX
        )
    y = exc.value.yielded
    assert y.event_key == "ask_user:s-1:tc-1"
    assert y.tool_name == "ts-1__ask"
    assert y.resume_metadata["source_version"] == 2
    assert y.resume_metadata["toolset_id"] == "ts-1"
    assert y.resume_metadata["tool_id"] == "ask"


@pytest.mark.asyncio
async def test_yielding_without_a_session_is_an_error_not_a_crash() -> None:
    res = await _provider().call(tool_name="ask", arguments={"question": "?"})
    assert res.is_error is True
    assert "session" in res.output


@pytest.mark.asyncio
async def test_the_resume_hook_is_registered_under_the_scoped_name() -> None:
    _provider()
    # Scoped because the registry is process-global and tool names are
    # operator-chosen: two toolsets defining 'ask' must not collide.
    assert get_resume_hook(scoped_tool_name("ts-1", "ask")) is python_tool_resume


@pytest.mark.asyncio
async def test_two_toolsets_can_both_define_ask() -> None:
    _provider()
    PythonToolsetProvider(
        toolset_id="ts-2",
        config=PythonConfig(source=SRC, source_version=1),
        runner=LocalHardenedRunner(),
    )
    assert get_resume_hook("ts-1__ask") is get_resume_hook("ts-2__ask")


@pytest.mark.asyncio
async def test_rebuilding_a_provider_does_not_trip_the_registry_guard() -> None:
    # register_resume_hook raises if a DIFFERENT hook claims a name. A closure
    # per tool would do exactly that on every rebuild.
    for _ in range(3):
        _provider()


@pytest.mark.asyncio
async def test_resume_runs_the_companion_and_returns_its_value() -> None:
    provider = _provider()
    res = await provider.resume_tool(
        tool_id="ask",
        payload={"response": "yes"},
        resume_metadata={"source_version": 2, "tool_meta": {}},
    )
    assert res.is_error is False
    assert res.output == "yes"


@pytest.mark.asyncio
async def test_resume_refuses_when_the_source_was_edited() -> None:
    provider = _provider(version=5)
    res = await provider.resume_tool(
        tool_id="ask",
        payload={"response": "yes"},
        resume_metadata={"source_version": 2, "tool_meta": {}},
    )
    assert res.is_error is True
    assert "edited" in res.output


@pytest.mark.asyncio
async def test_the_shared_hook_routes_through_the_resolver() -> None:
    provider = _provider()

    async def _resolve(tid: str):
        assert tid == "ts-1"
        return provider

    res = await python_tool_resume(
        {"source_version": 2, "tool_meta": {}, "toolset_id": "ts-1", "tool_id": "ask"},
        {"response": "routed"},
        ResumeContext(tool_name="ts-1__ask", tool_call_id="tc-1", resolve_provider=_resolve),
    )
    assert res.output == "routed"


@pytest.mark.asyncio
async def test_the_shared_hook_says_so_when_it_has_no_resolver() -> None:
    # The graph tool_call path passes None; the hook must explain rather than
    # crash on it.
    res = await python_tool_resume(
        {"source_version": 2, "toolset_id": "ts-1", "tool_id": "ask"},
        {"response": "x"},
        ResumeContext(tool_name="ts-1__ask", tool_call_id="tc-1"),
    )
    assert res.is_error is True
    assert "no toolset registry" in res.output


@pytest.mark.asyncio
async def test_a_bad_source_lists_no_tools_and_keeps_the_error() -> None:
    provider = _provider("def (")
    assert [t async for t in provider.list_tools()] == []
    assert provider.registration_error is not None
    assert provider.registration_error.lineno == 1


@pytest.mark.asyncio
async def test_the_provider_reports_its_isolation_level() -> None:
    assert _provider().isolation_level in {
        "container", "landlock", "seccomp", "sandbox-exec", "rlimit-only",
    }


# --- per-call runner selection (container workspaces) -------------------

WS1_CTX = ToolContext(tool_call_id="tc-1", session_id="s-1", workspace_id="ws-1")


class _MarkerRunner(Runner):
    """Recording stand-in for a sandbox runner: implements the real Runner
    contract (isolation_level + run) and never spawns a subprocess."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.gone_flag = False

    @property
    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.CONTAINER

    @property
    def gone(self) -> bool:
        return self.gone_flag

    async def run(self, request: dict[str, Any], *, timeout_seconds: float) -> ShimResponse:
        self.calls.append(request)
        return ShimResponse(ok=True, value=f"from-marker:{request['fn']}")


@pytest.mark.asyncio
async def test_the_sandbox_runner_cache_is_a_small_lru_that_closes_evicted_runners(
    monkeypatch,
) -> None:
    """A long-lived process must not keep one dead handle per distinct
    workspace id (X3 round 1, M1): the per-workspace cache is a small
    LRU. When it is at its bound the least-recently-used entry is evicted; a
    GONE (dead) runner is closed as it goes, but a live one is only dropped -
    a closed runtime client cannot reconnect, and the backend still owns a
    live workspace."""
    import primer.toolset.python_runner.provider as provider_mod

    monkeypatch.setattr(provider_mod, "_SANDBOX_RUNNER_CACHE_MAX", 2)
    closed: list[str] = []

    class _ClosingRunner(_MarkerRunner):
        def __init__(self, ws: str) -> None:
            super().__init__()
            self.ws = ws

        async def aclose(self) -> None:
            closed.append(self.ws)

    runners = {f"ws-{i}": _ClosingRunner(f"ws-{i}") for i in range(4)}

    async def resolve(workspace_id: str) -> _ClosingRunner:
        return runners[workspace_id]

    provider = _provider(sandbox_runner_for=resolve)
    await provider._runner_for(None, workspace_id="ws-0")
    await provider._runner_for(None, workspace_id="ws-1")
    await provider._runner_for(None, workspace_id="ws-0")  # a hit: ws-0 is now the most recent
    runners["ws-1"].gone_flag = True                        # ws-1's workspace went away in the meantime
    await provider._runner_for(None, workspace_id="ws-2")  # at the bound: the LRU (ws-1, gone) is evicted AND closed
    assert set(provider._sandbox_runners) == {"ws-0", "ws-2"}
    assert closed == ["ws-1"]
    await provider._runner_for(None, workspace_id="ws-3")  # at the bound again: ws-0 is the LRU, but LIVE
    assert set(provider._sandbox_runners) == {"ws-2", "ws-3"}
    assert closed == ["ws-1"]  # the live handle is dropped, never closed


@pytest.mark.asyncio
async def test_a_re_resolved_gone_runner_moves_to_the_mru_tail_and_releases_its_handle(
    monkeypatch,
) -> None:
    """X3 round 1b: re-resolving a workspace whose cached runner was GONE
    replaces the dead entry at the MRU tail (a just-used workspace is the
    LAST one a fill evicts), and the replaced handle is closed as it goes -
    its client's aiohttp session would otherwise linger for the process's
    life, the same leak the eviction path closes."""
    import primer.toolset.python_runner.provider as provider_mod

    monkeypatch.setattr(provider_mod, "_SANDBOX_RUNNER_CACHE_MAX", 2)
    closed: list[str] = []

    class _ClosingRunner(_MarkerRunner):
        def __init__(self, ws: str) -> None:
            super().__init__()
            self.ws = ws

        async def aclose(self) -> None:
            closed.append(self.ws)

    old0 = _ClosingRunner("ws-0")
    fresh0 = _ClosingRunner("ws-0")
    ws0_queue = [old0, fresh0]
    ws1 = _ClosingRunner("ws-1")
    ws2 = _ClosingRunner("ws-2")

    async def resolve(workspace_id: str) -> _ClosingRunner:
        if workspace_id == "ws-0":
            return ws0_queue.pop(0)  # the re-resolution hands back a fresh handle
        if workspace_id == "ws-1":
            return ws1
        return ws2

    provider = _provider(sandbox_runner_for=resolve)
    await provider._runner_for(None, workspace_id="ws-1")
    await provider._runner_for(None, workspace_id="ws-0")
    await provider._runner_for(None, workspace_id="ws-1")  # ws-1 is now the MRU; ws-0 the LRU
    old0.gone_flag = True                                  # ws-0's pod died: the handle self-evicts
    await provider._runner_for(None, workspace_id="ws-0")  # the gone entry re-resolves: a fresh handle replaces it
    await provider._runner_for(None, workspace_id="ws-2")  # at the bound: the LRU (ws-1) goes
    assert closed == ["ws-0"], (
        "the replaced GONE handle was not closed: its client's aiohttp session "
        "would linger for the process's life, like an unclosed eviction"
    )
    assert set(provider._sandbox_runners) == {"ws-0", "ws-2"}, (
        "the re-resolved workspace kept its old LRU position: the just-used "
        "runner was evicted before the older one"
    )


def _resolver(*runners: Any) -> Any:
    """A sandbox_runner_for that hands out the given entries, one per call:
    a Runner to use, ``None`` for a workspace without a sandbox, or an
    Exception to raise."""
    left = list(runners)

    async def resolve(workspace_id: str) -> _MarkerRunner | None:
        if not left:
            raise AssertionError(f"more resolutions than runners for {workspace_id}")
        runner = left.pop(0)
        if isinstance(runner, Exception):
            raise runner
        return runner

    return resolve


@pytest.mark.asyncio
async def test_a_call_from_a_container_workspace_runs_in_the_sandbox_runner() -> None:
    marker = _MarkerRunner()
    provider = _provider(sandbox_runner_for=_resolver(marker))
    result = await provider.call(
        tool_name="greet", arguments={"name": "ada"}, ctx=WS1_CTX
    )
    assert result.is_error is False
    assert result.output == "from-marker:greet"
    assert [c["fn"] for c in marker.calls] == ["greet"]


@pytest.mark.asyncio
async def test_a_call_with_a_workspace_but_no_wiring_runs_locally() -> None:
    result = await _provider().call(
        tool_name="greet", arguments={"name": "ada"}, ctx=WS1_CTX
    )
    assert result.is_error is False
    assert result.output == "hello ada"


@pytest.mark.asyncio
async def test_a_workspace_without_a_sandbox_falls_back_to_the_local_runner() -> None:
    marker = _MarkerRunner()
    provider = _provider(sandbox_runner_for=_resolver(None))
    result = await provider.call(
        tool_name="greet", arguments={"name": "ada"}, ctx=WS1_CTX
    )
    assert result.is_error is False
    assert result.output == "hello ada"
    assert marker.calls == []
    # The answer is cached: the second call does not re-ask the resolver.
    result2 = await provider.call(
        tool_name="greet", arguments={"name": "ada"}, ctx=WS1_CTX
    )
    assert result2.output == "hello ada"


@pytest.mark.asyncio
async def test_a_gone_sandbox_runner_is_reresolved_on_the_next_call() -> None:
    first, second = _MarkerRunner(), _MarkerRunner()
    provider = _provider(sandbox_runner_for=_resolver(first, second))
    r1 = await provider.call(tool_name="greet", arguments={"name": "a"}, ctx=WS1_CTX)
    assert r1.output == "from-marker:greet"
    first.gone_flag = True
    r2 = await provider.call(tool_name="greet", arguments={"name": "a"}, ctx=WS1_CTX)
    assert r2.output == "from-marker:greet"
    assert len(first.calls) == 1 and len(second.calls) == 1


@pytest.mark.asyncio
async def test_a_failed_sandbox_resolution_fails_the_call_not_the_local_runner() -> None:
    provider = _provider(
        sandbox_runner_for=_resolver(RuntimeError("container gone"), None)
    )
    r1 = await provider.call(tool_name="greet", arguments={"name": "ada"}, ctx=WS1_CTX)
    assert r1.is_error is True
    assert "ws-1" in r1.output and "container gone" in r1.output
    # The failed resolution is not cached: the next call re-asks the
    # resolver, gets no sandbox, and runs locally.
    r2 = await provider.call(tool_name="greet", arguments={"name": "ada"}, ctx=WS1_CTX)
    assert r2.is_error is False
    assert r2.output == "hello ada"


def test_the_container_isolation_flag_reflects_the_wiring() -> None:
    assert _provider().container_isolation is False
    assert _provider(sandbox_runner_for=_resolver(None)).container_isolation is True


@pytest.mark.asyncio
async def test_a_resume_rides_the_parking_workspace_runner() -> None:
    marker = _MarkerRunner()
    provider = _provider(sandbox_runner_for=_resolver(marker, marker))
    result = await provider.resume_tool(
        tool_id="ask",
        payload={"response": "yes"},
        resume_metadata={"source_version": 2, "workspace_id": "ws-1"},
    )
    assert result.is_error is False
    assert result.output == "from-marker:_ask_resume"
    assert [c["fn"] for c in marker.calls] == ["_ask_resume"]


@pytest.mark.asyncio
async def test_a_real_sandbox_runner_over_fake_sandbox_serves_a_python_tool(tmp_path) -> None:
    """The wiring end to end without a container: the real SandboxRunner
    (the real shim, the real exec contract) over the production FakeSandbox."""
    from primer.toolset.python_runner.runners import SandboxRunner
    from primer.workspace.sandbox.fake import FakeSandbox

    sandbox = FakeSandbox(tmp_path / "ws")

    async def resolve(workspace_id: str) -> SandboxRunner:
        return SandboxRunner(sandbox)

    provider = _provider(sandbox_runner_for=resolve)
    result = await provider.call(
        tool_name="greet", arguments={"name": "ada"}, ctx=WS1_CTX
    )
    assert result.is_error is False
    assert result.output == "hello ada"
