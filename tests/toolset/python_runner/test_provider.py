"""The provider: ToolsetProvider contract over the python runner."""

from __future__ import annotations

import pytest

from primer.model.providers.toolset import PythonConfig
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset.python_runner.provider import (
    PythonToolsetProvider,
    python_tool_resume,
    scoped_tool_name,
)
from primer.toolset.python_runner.runners import LocalHardenedRunner
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


def _provider(src: str = SRC, version: int = 2) -> PythonToolsetProvider:
    return PythonToolsetProvider(
        toolset_id="ts-1",
        config=PythonConfig(source=src, source_version=version),
        runner=LocalHardenedRunner(),
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
        "container", "seccomp", "sandbox-exec", "rlimit-only",
    }
