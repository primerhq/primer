"""G6-4: workspace ``grep`` runs the user's pattern bounded in size and time, off the event loop, on both backends.

``grep`` is the session file tool every agent has. Under stdlib ``re`` a pattern such as ``(a|aa)+$`` on one 37-character
line ran for seconds: inside the coroutine on the sandbox backend, and in a worker thread that still held the GIL on the
local one, so either way every session on the worker stalled. Each test measures the event loop while the grep runs.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import primer.workspace.local.tools.grep as grep_module
from primer.model.except_ import BadRequestError
from primer.workspace.local.tools.grep import Grep, GrepArgs
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.tools import SandboxGrep

HOSTILE = r"(a|aa)+$"  # exponential in both engines; about 5 s under stdlib re on this line
HOSTILE_LINE = "a" * 36 + "!"


class _LoopWatch:
    """The longest the event loop went without running a 5 ms ticker while the body ran."""

    def __init__(self) -> None:
        self.gaps: list[float] = [0.0]
        self._stop = False
        self._task: asyncio.Task | None = None

    async def _tick(self) -> None:
        last = time.perf_counter()
        while not self._stop:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            self.gaps.append(now - last)
            last = now

    async def __aenter__(self) -> "_LoopWatch":
        self._task = asyncio.create_task(self._tick())
        await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *exc) -> None:
        self._stop = True
        assert self._task is not None
        await self._task

    @property
    def max_gap(self) -> float:
        return max(self.gaps)


@pytest.fixture
def short_budget(monkeypatch):
    monkeypatch.setattr(grep_module, "GREP_MATCH_SECONDS", 0.3, raising=False)


async def _local(tmp_path: Path, body: str) -> Grep:
    (tmp_path / "f.txt").write_text(body)
    return Grep(tmp_path)


async def _sandbox(tmp_path: Path, body: str) -> SandboxGrep:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/f.txt", body.encode())
    return SandboxGrep(sb, workspace_root="/workspace")


BACKENDS = {"local": _local, "sandbox": _sandbox}


@pytest.mark.parametrize("backend", sorted(BACKENDS))
@pytest.mark.parametrize("multiline", [False, True], ids=["lines", "multiline"])
async def test_a_catastrophic_pattern_is_a_clear_error_and_the_loop_keeps_running(tmp_path, backend, multiline, short_budget):
    tool = await BACKENDS[backend](tmp_path, f"ok\n{HOSTILE_LINE}\n")
    started = time.perf_counter()
    async with _LoopWatch() as watch:
        with pytest.raises(BadRequestError, match="matching budget") as refused:
            await tool.execute(GrepArgs(pattern=HOSTILE, path=".", multiline=multiline), None)
    assert time.perf_counter() - started < 2.0
    assert watch.max_gap < 0.2, watch.max_gap
    assert "simplify the pattern" in str(refused.value)


@pytest.mark.parametrize("backend", sorted(BACKENDS))
async def test_a_pattern_over_1000_characters_is_refused(tmp_path, backend):
    tool = await BACKENDS[backend](tmp_path, "needle\n")
    with pytest.raises(BadRequestError, match="1001 characters long; the limit is 1000"):
        await tool.execute(GrepArgs(pattern="n" * 1001, path="."), None)


@pytest.mark.parametrize("backend", sorted(BACKENDS))
async def test_counted_repeats_that_never_finish_compiling_are_refused(tmp_path, backend):
    tool = await BACKENDS[backend](tmp_path, "needle\n")
    with pytest.raises(BadRequestError, match="repeats too much"):
        await tool.execute(GrepArgs(pattern="(?:a{65535}){65535}", path="."), None)


@pytest.mark.parametrize("backend", sorted(BACKENDS))
async def test_ordinary_patterns_answer_as_before(tmp_path, backend):
    tool = await BACKENDS[backend](tmp_path, "def alpha():\n    return 1\nDEF beta\n")
    content = await tool.execute(GrepArgs(pattern=r"^def \w+", path=".", output_mode="content"), None)
    assert content.output == "f.txt:1:def alpha():"
    folded = await tool.execute(GrepArgs(pattern=r"^def", path=".", output_mode="count", case_insensitive=True), None)
    assert folded.output == "f.txt:2"
    spanning = await tool.execute(GrepArgs(pattern=r"alpha.*return", path=".", output_mode="content", multiline=True), None)
    assert spanning.output == "f.txt::alpha():\\n    return"
    with pytest.raises(BadRequestError, match="invalid regex"):
        await tool.execute(GrepArgs(pattern="[unclosed", path="."), None)
