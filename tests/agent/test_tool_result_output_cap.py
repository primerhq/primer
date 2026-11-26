"""Review 2026-11-23 findings 2.6 + 5.4: every tool result the model sees
must fit the 50 KiB / 2000-line envelope.

Toolset results (MCP and internal) previously had no cap at all, and the
workspace cap was skipped when the tool itself reported ``truncated``
(a partial read of a 3,000-line file with 10 KiB lines returned 20 MB).
The cap sits at the single point in ``ToolExecutionManager`` every
result passes through (``_execute``), reuses the existing constants,
spills to the session's tmp cache when a session is bound, and reduces
to a byte-bounded head and tail otherwise. A preview is bounded in BYTES
as well as lines (a single 5 MiB line must not become the whole preview).

Round 1 (review 2026-11-23, 2.6 round 1):

* error results are capped with the SAME head+tail/spill treatment (a
  megabyte of error text is as much context as one of output), keeping
  ``error=True`` -- round 0 left them raw and its rebuild flipped the
  flag to ``False``;
* a workspace tool that bounded its own output (read/exec self-cap to
  ``TOOL_RESULT_CAP_BYTES``, ``truncated=True``) opts out WITH a hard
  ceiling (2 x the tool budget + 4 KiB): re-capping would spill what the
  tool deliberately kept (read's window note, exec's stderr tail);
  anything above the ceiling is capped whatever the flag says.
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from primer.agent.tool_manager import (
    _TOOL_OUTPUT_MAX_BYTES,
    _head_preview,
    _tail_preview,
    ToolExecutionManager,
)
from primer.model.chat import Tool, ToolCallPart, ToolCallResult
from primer.model.principal import PrincipalRef
from primer.workspace.local.tools._common import TOOL_RESULT_CAP_BYTES

_SYSTEM = PrincipalRef(type="system", id="test", display="test", source="local")
NOTES = "[the tool succeeded but the output was truncated]"
NOTES_ERROR = "[the tool failed and the error output was truncated]"

#: The self-capped opt-out ceiling: 2 x the tool's own budget + room for
#: its truncation note (the reviewer's formula, review 2026-11-23, 2.6
#: round 1).
_CEILING = 2 * TOOL_RESULT_CAP_BYTES + 4 * 1024


class _OutputToolset:
    """A one-tool toolset provider whose call returns a fixed output.

    Stands in for the MCP provider (output=joined text chunks) and the
    internal toolset (read_workspace_file and friends): both surface their
    results through ``provider.call`` and therefore through ``_execute``,
    where the cap lives."""

    lists_remotely = False

    def __init__(self, output: str, *, is_error: bool = False) -> None:
        self._output = output
        self._is_error = is_error

    async def list_tools(self, *, principal=None):
        yield Tool(id="big_tool", description="d", toolset_id="fake", args_schema={"type": "object"})

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def call(self, *, tool_name, arguments, principal=None, ctx=None):
        return ToolCallResult(output=self._output, is_error=self._is_error)


class _WsTool:
    """A workspace tool that returns ``output`` and reports
    ``truncated=truncated`` (the 5.4 skip case sets both)."""

    def __init__(
        self,
        output: str,
        *,
        truncated: bool = False,
        metadata: dict | None = None,
        self_bounded: bool = False,
    ) -> None:
        self.self_bounded = self_bounded
        self.id = "big"
        self.description = "a big tool"
        self.examples: list = []
        self._output = output
        self._truncated = truncated
        self._metadata = metadata

    def parameters(self) -> type[BaseModel]:
        class Args(BaseModel):
            pass

        return Args

    async def execute(self, args, ctx):
        return NS(output=self._output, truncated=self._truncated, metadata=self._metadata)


def _session() -> NS:
    return NS(
        session_id="s",
        workspace_id="w",
        agent_id="a",
        cache_output=AsyncMock(return_value="/tmp/tool_cache.txt"),
    )


def _toolset_manager(output: str, *, session: NS | None, is_error: bool = False) -> ToolExecutionManager:
    return ToolExecutionManager(
        toolset_providers={"fake": _OutputToolset(output, is_error=is_error)},
        workspace_session=session,
        initiated_by=_SYSTEM,
    )


async def _run_toolset(manager: ToolExecutionManager) -> ToolCallResult:
    return await manager.execute(ToolCallPart(id="tc1", name="fake__big_tool", arguments={}))


def _ws_manager(
    output: str,
    *,
    session: NS | None,
    truncated: bool,
    metadata: dict | None = None,
    self_bounded: bool = False,
) -> ToolExecutionManager:
    return ToolExecutionManager(
        workspace_tools={
            "big": _WsTool(
                output, truncated=truncated, metadata=metadata,
                self_bounded=self_bounded,
            )
        },
        workspace_session=session,
        initiated_by=_SYSTEM,
    )


async def _run_ws(manager: ToolExecutionManager) -> ToolCallResult:
    return await manager.execute(ToolCallPart(id="tc1", name="workspace__big", arguments={}))


@pytest.mark.asyncio
async def test_large_toolset_result_capped_with_session() -> None:
    """An MCP/internal toolset result over the envelope is capped and
    spilled to the session cache (2.6)."""
    output = "x" * 200_000
    session = _session()
    result = await _run_toolset(_toolset_manager(output, session=session))

    assert result.error is False
    assert NOTES in result.output
    assert "Full output saved to: /tmp/tool_cache.txt" in result.output
    # The full output went to the cache; the model sees the head.
    session.cache_output.assert_awaited_once_with(output)
    assert result.output.startswith("xxxxx")
    assert len(result.output.encode("utf-8")) < 100_000


@pytest.mark.asyncio
async def test_large_toolset_result_capped_without_session() -> None:
    """No bound session (chat path): the model gets a byte-bounded head and
    tail instead of a cache spill."""
    output = "y" * 200_000
    result = await _run_toolset(_toolset_manager(output, session=None))

    assert result.error is False
    assert "the output was truncated" in result.output
    assert "Full output saved to" not in result.output
    assert "End of the output" in result.output
    assert len(result.output.encode("utf-8")) < 150_000


@pytest.mark.asyncio
async def test_small_toolset_result_unchanged() -> None:
    output = "all good"
    result = await _run_toolset(_toolset_manager(output, session=None))
    assert result.output == "all good"
    assert result.error is False


@pytest.mark.asyncio
async def test_error_toolset_result_capped_with_session() -> None:
    """Round 1: an error result over the envelope gets the SAME head/spill
    treatment as a success, keeping ``error=True`` (round 0 left errors raw
    -- ``test_error_toolset_result_not_capped`` locked that in -- and its
    rebuild flipped the flag to ``False``)."""
    output = "e" * 200_000
    session = _session()
    result = await _run_toolset(
        _toolset_manager(output, session=session, is_error=True)
    )
    assert result.error is True
    assert NOTES_ERROR in result.output
    assert "the tool succeeded" not in result.output
    assert "Full output saved to: /tmp/tool_cache.txt" in result.output
    session.cache_output.assert_awaited_once_with(output)
    assert len(result.output.encode("utf-8")) < 100_000


@pytest.mark.asyncio
async def test_error_toolset_result_capped_without_session() -> None:
    """Round 1: the chat-path error cap is a byte-bounded head and tail,
    with the error-aware note."""
    output = "e" * 200_000
    result = await _run_toolset(_toolset_manager(output, session=None, is_error=True))
    assert result.error is True
    assert "the tool failed and the error output was truncated" in result.output
    assert "the tool succeeded" not in result.output
    assert "End of the output" in result.output
    assert "Full output saved to" not in result.output
    assert len(result.output.encode("utf-8")) < 150_000


@pytest.mark.asyncio
async def test_workspace_truncated_result_still_capped() -> None:
    """5.4 stays closed at the ceiling: a workspace result reporting
    truncated=True ABOVE it (200 KB here; the real read/exec self-cap is
    TOOL_RESULT_CAP_BYTES) is capped whatever the flag says."""
    output = "z" * 200_000
    session = _session()
    result = await _run_ws(_ws_manager(output, session=session, truncated=True, self_bounded=True))

    assert result.error is False
    assert NOTES in result.output
    assert "Full output saved to: /tmp/tool_cache.txt" in result.output
    session.cache_output.assert_awaited_once_with(output)
    assert len(result.output.encode("utf-8")) < 100_000


@pytest.mark.asyncio
async def test_a_truncated_result_of_a_tool_that_does_not_bound_is_capped() -> None:
    """The 2x self-cap allowance belongs to ``self_bounded`` tools: a workspace result
    that reports ``truncated`` (grep's head_limit / search caps) but whose tool does
    NOT bound its own output (``self_bounded`` False) takes the standard envelope even
    under the 2x ceiling (X3.5: grep/glob rode the ``truncated`` flag into the
    self-cap opt-out and got 104 KiB instead of the 50 KiB envelope)."""
    output = "g" * 80_000  # over the 50 KiB envelope, under the 2x ceiling (106 KiB)
    session = _session()
    result = await _run_ws(_ws_manager(output, session=session, truncated=True))

    assert result.error is False
    assert NOTES in result.output
    assert "Full output saved to: /tmp/tool_cache.txt" in result.output
    session.cache_output.assert_awaited_once_with(output)


@pytest.mark.asyncio
async def test_workspace_small_result_unchanged() -> None:
    """Benign: a workspace result under the envelope passes through as is,
    truncated flag or not."""
    session = _session()
    result = await _run_ws(_ws_manager("small", session=session, truncated=True))
    assert result.output == "small"
    session.cache_output.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_non_truncated_large_result_still_capped() -> None:
    """The opt-out requires the flag: a workspace result that did NOT bound
    its own output (truncated=False) over the envelope is capped, as 5.4
    requires of every tool."""
    output = "w" * 200_000
    session = _session()
    result = await _run_ws(_ws_manager(output, session=session, truncated=False))
    assert NOTES in result.output
    session.cache_output.assert_awaited_once_with(output)


def _read_default_window() -> str:
    """Faithful to the reviewer's probe: a default ``read`` of a 2,500-line
    file. The default window is cut at TOOL_RESULT_CAP_BYTES of ENCODED
    bytes, so the result sits just OVER the outer byte cap (931 lines,
    51,245 B in the probe) with the window-cut note, and reports
    truncated=True."""
    lines = [f"   {i:5d}\u2192 word {i} " + "x" * 36 for i in range(1, 932)]
    body = "\n".join(lines)
    # Pad the note so the encoded body is just over the 50 KiB byte cap.
    note = (
        "\n[window cut: the file continues past this window; "
        "read with offset=931 to continue] " + "#" * 40
    )
    body = body + note
    assert len(body.encode("utf-8")) > _TOOL_OUTPUT_MAX_BYTES
    assert len(body.encode("utf-8")) <= _CEILING
    assert body.count("\n") <= 2000
    return body


@pytest.mark.asyncio
async def test_read_default_window_not_re_capped() -> None:
    """Round 1 probe: default read of a 2,500-line file (931 lines inline,
    51 KB, window-cut note, truncated=True). Round 0 re-capped and re-spilled
    it because it sits just over the byte cap; the self-capped opt-out must
    pass it through unchanged."""
    output = _read_default_window()
    session = _session()
    result = await _run_ws(_ws_manager(output, session=session, truncated=True, self_bounded=True))
    assert result.output == output, "the self-capped read window was re-capped"
    session.cache_output.assert_not_awaited()
    assert result.metadata is None, "the reserved flag must not leak into metadata"


def _read_short_lines_window() -> str:
    """Faithful to the reviewer's probe: ``read limit=2500`` on a file of
    short lines (36 KB, 2,500 lines): under the byte cap but OVER the
    2000-line cap, with truncated=True."""
    lines = [f"   {i:5d}\u2192 ok" for i in range(1, 2501)]
    body = "\n".join(lines) + "\n[window: 2,500 of 4,000 lines]"
    assert len(body.encode("utf-8")) < _TOOL_OUTPUT_MAX_BYTES
    assert body.count("\n") > 2000
    return body


@pytest.mark.asyncio
async def test_read_short_lines_window_not_re_capped() -> None:
    """Round 1 probe: read limit=2500 on short lines (36 KB, >2000 lines,
    truncated=True). Round 0 spilled it for >2000 lines; the self-capped
    opt-out skips BOTH the byte and the line check."""
    output = _read_short_lines_window()
    session = _session()
    result = await _run_ws(_ws_manager(output, session=session, truncated=True, self_bounded=True))
    assert result.output == output, "the self-capped read window was re-capped"
    session.cache_output.assert_not_awaited()


def _exec_fatal_output() -> str:
    """Faithful to the reviewer's probe: ``exec`` with 71 KB of stdout and a
    one-line FATAL on stderr. The exec self-cap keeps 40 KiB head + 10 KiB
    tail of EACH stream (head + tail = TOOL_RESULT_CAP_BYTES per stream), so
    the result is just over the outer byte cap with the omission marker, the
    FATAL line intact, and truncated=True."""
    stdout_head = "O" * (40 * 1024)
    stdout_tail = "o" * (10 * 1024)
    omitted = 71 * 1024 - 50 * 1024
    body = (
        "1\n"
        + stdout_head
        + f"\n[... {omitted} bytes omitted ...]\n"
        + stdout_tail
        + "\n"
        + "FATAL: build failed at step 7"
        + f"\n[output truncated: {omitted} bytes were omitted from the streams above]"
    )
    assert len(body.encode("utf-8")) > _TOOL_OUTPUT_MAX_BYTES
    assert len(body.encode("utf-8")) <= _CEILING
    return body


@pytest.mark.asyncio
async def test_exec_fatal_stderr_stays_visible() -> None:
    """Round 1 probe: exec 71 KB stdout + one-line FATAL on stderr (self-capped
    to ~51 KB, truncated=True). Round 0 re-spilled the whole result and hid
    the FATAL; the self-capped opt-out must keep it inline."""
    output = _exec_fatal_output()
    session = _session()
    result = await _run_ws(
        _ws_manager(
            output, session=session, truncated=True,
            metadata={"exit_code": 1, "command": "make"},
            self_bounded=True,  # the real Exec tool is self_bounded (ClassVar)
        )
    )
    assert "FATAL: build failed at step 7" in result.output
    assert result.output == output, "the self-capped exec output was re-capped"
    session.cache_output.assert_not_awaited()
    # The tool's own metadata survives; the reserved flag does not.
    assert result.metadata == {"exit_code": 1, "command": "make"}


def test_single_giant_line_preview_is_byte_bounded() -> None:
    """5.4: a single 5 MiB line must not become the preview. Round 1: each
    preview slices its own window off the output first (no whole-output
    splitlines), so head and tail each stay at or under the byte budget."""
    line = "Z" * 5_000_000
    head = _head_preview(line)
    tail = _tail_preview(line)
    assert len(head.encode("utf-8")) <= _TOOL_OUTPUT_MAX_BYTES
    assert len(tail.encode("utf-8")) <= _TOOL_OUTPUT_MAX_BYTES
    # The tail is the END of the line (not the head again): it starts where
    # the 5 MiB line ends.
    assert line.endswith(tail)


@pytest.mark.asyncio
async def test_single_giant_line_result_capped() -> None:
    """The exec-cat-bundle.min.js scenario, end to end through the cap.
    Round 1: head and tail are each byte-bounded (the tail used to degrade
    to nothing for a single over-budget line, so only the head was
    asserted); together they stay within twice the budget plus the note."""
    line = "Z" * 5_000_000
    session = _session()
    result = await _run_toolset(_toolset_manager(line, session=session))
    assert result.output.count("Z") <= 2 * _TOOL_OUTPUT_MAX_BYTES + 1
    assert NOTES in result.output


def test_tail_preview_starts_on_a_line_boundary() -> None:
    """Round 1: the tail slices output[-MAX:] first and drops the slice's
    leading partial line (the cut lands mid-line)."""
    lines = [f"line {i:04d}" for i in range(4000)]
    output = "\n".join(lines)
    tail = _tail_preview(output)
    tail_lines = tail.splitlines()
    assert len(tail_lines) == 50
    # Every tail line is a WHOLE output line, and they are the last 50.
    assert tail_lines == lines[-50:]


def test_preview_line_count_bound_holds() -> None:
    lines = [f"line {i}" for i in range(3000)]
    head = _head_preview("\n".join(lines))
    assert head.splitlines()[:1] == ["line 0"]
    assert len(head.splitlines()) == 50
    assert head.splitlines()[-1] == "line 49"


# --- F2 finding 1: a self-bounded tool (read/exec) that cut NOTHING --------


@pytest.mark.asyncio
async def test_self_bounded_untruncated_read_window_stays_inline() -> None:
    """``read limit=2500`` of a file of exactly 2,500 lines: nothing cut
    (truncated=False) but over the 2000-line cap. The tool bounds its own
    output, so the outer cap must not spill it behind a preview."""
    output = "\n".join(f"   {i:5d}\u2192 ok" for i in range(1, 2501))
    assert output.count("\n") > 2000
    session = _session()
    result = await _run_ws(
        _ws_manager(output, session=session, truncated=False, self_bounded=True)
    )
    assert result.output == output
    session.cache_output.assert_not_awaited()
    assert result.metadata is None


@pytest.mark.asyncio
async def test_self_bounded_untruncated_exec_keeps_fatal_visible() -> None:
    """``exec "seq 3000; echo FATAL >&2"``: 3000 lines, nothing cut, FATAL
    on stderr. It must stay inline with FATAL visible."""
    output = "\n".join(str(i) for i in range(1, 3001)) + "\nFATAL"
    session = _session()
    result = await _run_ws(
        _ws_manager(output, session=session, truncated=False, self_bounded=True)
    )
    assert "FATAL" in result.output
    assert result.output == output
    session.cache_output.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_bounded_untruncated_exec_49k_stdout_5k_stderr() -> None:
    """49 KB stdout + 5 KB stderr (54 KB, nothing cut): over the 50 KiB cap,
    under the ceiling; the stderr line must not be spilled."""
    output = "o" * 49_000 + "\nSTDERR-LINE\n" + "e" * 5_000
    assert len(output.encode("utf-8")) > _TOOL_OUTPUT_MAX_BYTES
    session = _session()
    result = await _run_ws(
        _ws_manager(output, session=session, truncated=False, self_bounded=True)
    )
    assert "STDERR-LINE" in result.output
    assert result.output == output
    session.cache_output.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_bounded_above_ceiling_still_capped() -> None:
    """The hard ceiling still applies to a self-bounded tool, truncated or not."""
    output = "y" * (_CEILING + 1)
    for truncated in (False, True):
        session = _session()
        result = await _run_ws(
            _ws_manager(output, session=session, truncated=truncated, self_bounded=True)
        )
        assert NOTES in result.output
        session.cache_output.assert_awaited_once_with(output)


@pytest.mark.asyncio
async def test_grep_like_untruncated_tool_unchanged() -> None:
    """A tool that is NOT self-bounded (grep, glob) and cut nothing is still
    capped over the envelope, exactly as before."""
    output = "\n".join(f"match {i}" for i in range(2500))
    session = _session()
    result = await _run_ws(_ws_manager(output, session=session, truncated=False))
    assert NOTES in result.output
    session.cache_output.assert_awaited_once_with(output)


def test_read_and_exec_declare_self_bounded() -> None:
    from primer.workspace.local.tools.exec_ import Exec
    from primer.workspace.local.tools.glob import Glob
    from primer.workspace.local.tools.grep import Grep
    from primer.workspace.local.tools.read import Read

    assert Read.self_bounded is True and Exec.self_bounded is True
    assert Grep.self_bounded is False and Glob.self_bounded is False
