"""Characterization of a representative python toolset, pinned before the shim hardening (Part 3 Task 105).

Each case is an existing toolset shape; the exact output and error text must not move when the shim's seccomp filter,
output handling or stdout wiring changes: a pure function, JSON in and out, a raising tool, a timeout, a memory-limit
failure, an async tool, a yielding tool, and a tool that uses threads (``clone`` with ``CLONE_THREAD``).
"""

from __future__ import annotations

import json

import pytest

from primer.model.providers.toolset import PythonConfig
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset.python_runner.provider import PythonToolsetProvider
from primer.toolset.python_runner.runners import LocalHardenedRunner

_DOC = '    """Do it.\n\n    Use when testing.\n\n    Args:\n        a: A.\n    """\n'

SRC = (
    "import json, threading, time\n"
    "from concurrent.futures import ThreadPoolExecutor\n"
    "\n"
    "@primer_tool()\n"
    "def pure(a: str) -> str:\n" + _DOC + "    return 'hello ' + a\n"
    "\n"
    "@primer_tool()\n"
    "def shape(a: dict) -> dict:\n" + _DOC + "    return {'keys': sorted(a), 'n': len(a), 'nested': {'x': [1, 2.5, None, True]}}\n"
    "\n"
    "@primer_tool()\n"
    "def boom(a: str) -> str:\n" + _DOC + "    raise ValueError('nope: ' + a)\n"
    "\n"
    "@primer_tool(timeout_seconds=1)\n"
    "def nap(a: str) -> str:\n" + _DOC + "    time.sleep(30)\n    return 'woke'\n"
    "\n"
    "@primer_tool()\n"
    "def hog(a: str) -> str:\n" + _DOC + "    data = bytearray(1024 * 1024 * 1024)\n    return str(len(data))\n"
    "\n"
    "@primer_tool()\n"
    "async def later(a: str) -> str:\n" + _DOC + "    return 'async ' + a\n"
    "\n"
    "@primer_tool()\n"
    "def ask(a: str, ctx) -> str:\n" + _DOC + "    return ask_user('ship ' + a + '?')\n"
    "\n"
    "@primer_tool()\n"
    "def threads(a: str) -> list:\n" + _DOC
    + "    out = []\n"
    "    t = threading.Thread(target=lambda: out.append('thread ' + a))\n"
    "    t.start()\n"
    "    t.join()\n"
    "    with ThreadPoolExecutor(max_workers=4) as pool:\n"
    "        out.extend(pool.map(lambda i: i * i, range(8)))\n"
    "    return out\n"
)

CTX = ToolContext(tool_call_id="tc-1", session_id="s-1", workspace_id=None)


def _provider() -> PythonToolsetProvider:
    return PythonToolsetProvider(
        toolset_id="ts-c", config=PythonConfig(source=SRC, source_version=1), runner=LocalHardenedRunner(),
    )


async def test_a_pure_function_returns_its_string() -> None:
    res = await _provider().call(tool_name="pure", arguments={"a": "ada"})
    assert (res.is_error, res.output) == (False, "hello ada")


async def test_json_in_and_out() -> None:
    res = await _provider().call(tool_name="shape", arguments={"a": {"b": 1, "a": [2]}})
    assert res.is_error is False
    assert res.output == json.dumps({"keys": ["a", "b"], "n": 2, "nested": {"x": [1, 2.5, None, True]}})


async def test_a_raising_tool_answers_type_and_message() -> None:
    res = await _provider().call(tool_name="boom", arguments={"a": "x"})
    assert (res.is_error, res.output) == (True, "ValueError: nope: x")
    assert res.extended["traceback"].endswith("ValueError: nope: x\n")


async def test_a_timeout_answers_the_fixed_text() -> None:
    res = await _provider().call(tool_name="nap", arguments={"a": "x"})
    assert (res.is_error, res.output) == (True, "TimeoutError: the tool exceeded its timeout and was killed")
    assert res.extended == {"traceback": ""}


async def test_a_memory_limit_failure_answers_memory_error() -> None:
    res = await _provider().call(tool_name="hog", arguments={"a": "x"})
    assert (res.is_error, res.output) == (True, "MemoryError: ")
    assert "MemoryError" in res.extended["traceback"]


async def test_an_async_tool_is_awaited() -> None:
    res = await _provider().call(tool_name="later", arguments={"a": "x"})
    assert (res.is_error, res.output) == (False, "async x")


async def test_a_yielding_tool_parks() -> None:
    with pytest.raises(YieldToWorker) as exc:
        await _provider().call(tool_name="ask", arguments={"a": "it"}, ctx=CTX)
    y = exc.value.yielded
    assert (y.tool_name, y.event_key) == ("ts-c__ask", "ask_user:s-1:tc-1")
    assert y.resume_metadata["params"] == {"question": "ship it?"}


async def test_threads_still_work() -> None:
    res = await _provider().call(tool_name="threads", arguments={"a": "x"})
    assert (res.is_error, json.loads(res.output)) == (False, ["thread x", 0, 1, 4, 9, 16, 25, 36, 49])
