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
async def test_counted_repeats_past_the_cap_are_refused(tmp_path, backend):
    # The blow-up shapes themselves run through compile_grep_pattern in a child process
    # (tests/common/test_user_regex_entry_points.py); this one compiles in milliseconds even if the check regressed.
    tool = await BACKENDS[backend](tmp_path, "needle\n")
    with pytest.raises(BadRequestError, match="repeats too much"):
        await tool.execute(GrepArgs(pattern="(?:a{101}){100}", path="."), None)


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



# ---- round 2, M4: the error names its cause; L1: the dedicated executor; the prefilter keeps the answers ----------------

# \Z tests the string end, so this pattern gets no whole-text prefilter: each line is searched on its own.
PER_LINE_HOSTILE = r"(a|aa)+\Z"


@pytest.mark.parametrize("backend", sorted(BACKENDS))
async def test_one_search_past_the_per_search_bound_is_named(tmp_path, backend, monkeypatch):
    monkeypatch.setattr(grep_module, "GREP_PER_SEARCH_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(grep_module, "GREP_MATCH_SECONDS", 5.0, raising=False)
    tool = await BACKENDS[backend](tmp_path, f"ok\n{HOSTILE_LINE}\n")
    with pytest.raises(BadRequestError) as refused:
        await tool.execute(GrepArgs(pattern=PER_LINE_HOSTILE, path="."), None)
    message = str(refused.value)
    assert "f.txt line 2" in message, message
    assert "one search ran past the 0.05 s per-search bound" in message, message


@pytest.mark.parametrize("backend", sorted(BACKENDS))
async def test_a_spent_budget_says_how_far_grep_got_and_its_longest_search(tmp_path, backend, monkeypatch):
    monkeypatch.setattr(grep_module, "GREP_MATCH_SECONDS", 0.3, raising=False)
    slow_line = "a" * 26 + "!"  # about 0.1 s each in the regex engine
    tool = await BACKENDS[backend](tmp_path, "\n".join(["ok", "fine"] + [slow_line] * 20) + "\n")
    with pytest.raises(BadRequestError) as refused:
        await tool.execute(GrepArgs(pattern=PER_LINE_HOSTILE, path="."), None)
    message = str(refused.value)
    assert "grep stopped after " in message and " lines" in message, message
    assert "matching used its 0.3 s matching budget" in message, message
    assert "the longest single search took" in message, message


@pytest.mark.parametrize("backend", sorted(BACKENDS))
async def test_grep_matches_on_the_user_regex_executor(tmp_path, backend, monkeypatch):
    import threading

    import primer.workspace.sandbox.tools.grep as sandbox_grep_module

    names: list[str] = []
    real = grep_module.grep_text

    def _recording(*args, **kwargs):
        names.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(grep_module, "grep_text", _recording)
    monkeypatch.setattr(sandbox_grep_module, "grep_text", _recording)
    tool = await BACKENDS[backend](tmp_path, "needle\n")
    result = await tool.execute(GrepArgs(pattern="need", path="."), None)
    assert result.output == "f.txt"
    assert names and all(name.startswith("user-regex") for name in names), names


@pytest.mark.parametrize("backend", sorted(BACKENDS))
async def test_the_whole_text_prefilter_keeps_every_answer(tmp_path, backend):
    body = "\r\n".join(
        ["def alpha():", "    return 1", "", "x = alpha()  # TODO", "class Beta:", "  def alpha(self): pass", "TODO"]
    )
    tool = await BACKENDS[backend](tmp_path, body + "\n")
    cases = {
        (r"^\s*def \w+", "content", 0): "f.txt:1:def alpha():\nf.txt:6:  def alpha(self): pass",
        (r"TODO$", "count", 0): "f.txt:2",
        (r"alpha\(\)", "content", 1): "f.txt:1:def alpha():\nf.txt:2:    return 1\nf.txt:3:\nf.txt:4:x = alpha()  # TODO\nf.txt:5:class Beta:",
        (r"\bBeta\b", "files_with_matches", 0): "f.txt",
        (r"^$", "count", 0): "f.txt:1",
    }
    for (pattern, mode, context), expected in cases.items():
        result = await tool.execute(GrepArgs(pattern=pattern, path=".", output_mode=mode, context=context), None)
        assert result.output == expected, (pattern, mode, result.output)
