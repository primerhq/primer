"""Review 2026-11-23 finding 5.3 (local half), fix round 1 part 2:
``exec`` output is bounded PER STREAM, head AND tail.

``exec`` used to ``communicate()`` the whole of a command's stdout/stderr
into the API/worker process. A single shared head budget would let one
stream starve the other (50 KiB of stdout erasing a failing stderr), so
each stream keeps its own head (``_HEAD_BYTES``, 40 KiB) and tail
(``_TAIL_BYTES``, 10 KiB -- head + tail = ``TOOL_RESULT_CAP_BYTES``),
with a ``[... N bytes omitted ...]`` marker between them. The pipes are
still drained to EOF (a child must never block on a full pipe), and the
result sets ``truncated=True`` whenever bytes were omitted so the
runtime skips its outer truncation.

Fix round 2: the omitted count is only the bytes actually discarded
(size - head - tail): round 1 counted the tail's kept bytes into it,
overstating the omission by up to the tail budget and marking a
head-plus-tail-sized stream (nothing dropped) as truncated. A stream
that fits its head + tail budgets now comes back whole, unmarked, and
unflagged.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

import pytest

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.model.except_ import BadRequestError
from primer.workspace.local.tools import Exec, ExecArgs
from primer.workspace.local.tools._common import TOOL_RESULT_CAP_BYTES


async def test_a_printing_command_beyond_the_cap_returns_bounded(
    tmp_path: Path
) -> None:
    """Far more than the cap is printed; the result is bounded, names the
    omission, and says it was truncated (assert on the result, not RSS)."""
    result = await Exec(tmp_path).execute(
        ExecArgs(
            command="yes abcdefghijklmnop | head -c 1048576",
            description="print 1MiB",
            timeout_ms=30000,
        ),
        None,
    )
    out = result.output
    # The head of the output survived...
    assert out.startswith("0\n" + "abcdefghijklmnop")
    # ...the middle is named as omitted...
    assert "[... " in out
    assert "bytes omitted" in out
    assert "[output truncated" in out
    # ...and the result stays within the stream budget (head + tail) plus
    # the markers and the note:
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + 512
    # ...and the tool flagged its own truncation:
    assert result.truncated is True


async def test_a_stream_keeps_its_tail(tmp_path: Path) -> None:
    """The LAST line of a long stream survives (it is the end of a
    failing build log)."""
    result = await Exec(tmp_path).execute(
        ExecArgs(
            command='for i in $(seq 1 20000); do echo "build step $i"; done',
            description="20000 build steps",
            timeout_ms=30000,
        ),
        None,
    )
    out = result.output
    assert "build step 1" in out  # the head survived
    assert "build step 20000" in out  # the last line (tail) survived
    assert "[... " in out
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + 512


async def test_a_huge_stdout_does_not_starve_stderr(tmp_path: Path) -> None:
    """A full stdout budget must not erase a short failing stderr."""
    result = await Exec(tmp_path).execute(
        ExecArgs(
            command="yes abcdefghijklmnop | head -c 1048576; echo ERR-SENTINEL 1>&2",
            description="1MiB stdout plus one stderr line",
            timeout_ms=30000,
        ),
        None,
    )
    out = result.output
    assert "ERR-SENTINEL" in out  # the stderr line survived intact
    assert out.startswith("0\nabcdefghijklmnop")
    assert "[... " in out  # the stdout middle was omitted
    assert result.truncated is True


async def test_check_true_failure_carries_the_stderr_tail(tmp_path: Path) -> None:
    """A failing build with a long stderr: the check=True failure message
    must end with the END of stderr (where the error lands), not a head.
    A head-only budget would end the message with the noise lines."""
    sentinel = f"linker-error-{uuid.uuid4().hex[:8]}"
    tail = f"BUILD FAILED: {sentinel} at step 50000"
    with pytest.raises(BadRequestError) as excinfo:
        await Exec(tmp_path).execute(
            ExecArgs(
                command=(
                    "for i in $(seq 1 50000); do echo noise; done 1>&2; "
                    f'echo "{tail}" 1>&2; '
                    "exit 3"
                ),
                description="failing build with a long stderr",
                check=True,
                timeout_ms=30000,
            ),
            None,
        )
    msg = str(excinfo.value)
    assert "command exited 3" in msg
    assert msg.rstrip().endswith(tail)


async def test_small_output_is_unchanged_and_untagged(tmp_path: Path) -> None:
    """Under the cap nothing is dropped, no note appears, and the result
    is NOT flagged truncated."""
    result = await Exec(tmp_path).execute(
        ExecArgs(command="echo hello; echo err 1>&2", description="small",
                 timeout_ms=10000),
        None,
    )
    assert result.output == "0\nhello\n\nerr\n"
    assert "truncated" not in result.output
    assert result.truncated is False


async def test_stderr_is_drained_and_counted_too(tmp_path: Path) -> None:
    """A stream past the cap on stderr alone still returns, bounded, with
    the omission named -- and does not hang on a full pipe."""
    result = await Exec(tmp_path).execute(
        ExecArgs(
            command="yes z | head -c 524288 1>&2",
            description="512KiB on stderr",
            timeout_ms=30000,
        ),
        None,
    )
    out = result.output
    assert out.startswith("0\n")
    assert "[... " in out
    assert "[output truncated" in out
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + 512
    assert result.truncated is True


async def test_truncated_flag_set_only_when_bytes_omitted(tmp_path: Path) -> None:
    """The runtime skips outer truncation only when the tool already
    bounded its output."""
    big = await Exec(tmp_path).execute(
        ExecArgs(
            command="yes abcdefghijklmnop | head -c 1048576",
            description="big",
            timeout_ms=30000,
        ),
        None,
    )
    assert big.truncated is True

    small = await Exec(tmp_path).execute(
        ExecArgs(command="echo hello", description="small", timeout_ms=10000),
        None,
    )
    assert small.truncated is False


async def test_a_timeout_command_is_still_killed_and_reported(tmp_path: Path) -> None:
    """The cap must not swallow the timeout path."""
    with pytest.raises(BadRequestError, match="timed out"):
        await Exec(tmp_path).execute(
            ExecArgs(command="sleep 30", description="long", timeout_ms=500),
            None,
        )


async def test_a_printing_command_is_reaped_on_timeout(tmp_path: Path) -> None:
    """Printing continuously when the timeout fires: the call returns
    within the timeout plus a margin, and the process group is gone."""
    marker = f"c1-reap-{uuid.uuid4().hex[:8]}"
    t0 = time.monotonic()
    with pytest.raises(BadRequestError, match="timed out"):
        await Exec(tmp_path).execute(
            ExecArgs(
                command=f"while :; do echo {marker}; done",
                description="spin forever",
                timeout_ms=500,
            ),
            None,
        )
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert elapsed_ms < 500 + 3000, f"timed-out exec took {elapsed_ms:.0f}ms"
    pgrep = subprocess.run(["pgrep", "-f", marker], capture_output=True)
    assert pgrep.returncode != 0, "the timed-out process group is still alive"


# ---------------------------------------------------------------------------
# Fix round 2: the omitted count is only the bytes actually discarded.
# ---------------------------------------------------------------------------


async def test_a_head_plus_tail_sized_stream_is_returned_whole_untagged(
    tmp_path: Path,
) -> None:
    """Round 2: a stream of exactly head + tail size (45 KiB = 40960 +
    10240) drops NOTHING, so it comes back whole, unmarked, and NOT
    flagged truncated -- round 1 counted the tail-kept bytes into
    'dropped' and falsely reported 5120 omitted bytes."""
    total = 40960 + 10240  # exactly the per-stream kept budget
    blob = "abcdefghijklmnop" * (total // 16)  # exactly 46080 bytes
    (tmp_path / "blob45.bin").write_text(blob)
    result = await Exec(tmp_path).execute(
        ExecArgs(
            command="cat blob45.bin",
            description="print exactly head+tail bytes",
            timeout_ms=30000,
        ),
        None,
    )
    # The WHOLE stream, byte for byte: head + tail, no marker between.
    assert result.output == "0\n" + blob + "\n"
    assert "[... " not in result.output
    assert "[output truncated" not in result.output
    assert result.truncated is False


async def test_the_omitted_count_is_exactly_the_discarded_middle(
    tmp_path: Path,
) -> None:
    """Round 2: for a 1 MiB stream the omitted count is size - head -
    tail (997376), not size - head (1007616) -- round 1 counted the
    tail's kept bytes as dropped too."""
    total = 1048576
    head, tail = 40 * 1024, 10 * 1024
    result = await Exec(tmp_path).execute(
        ExecArgs(
            command=f"yes abcdefghijklmnop | head -c {total}",
            description="print 1MiB",
            timeout_ms=30000,
        ),
        None,
    )
    out = result.output
    assert f"[... {total - head - tail} bytes omitted ...]" in out
    assert f"[... {total - head} bytes omitted ...]" not in out
    assert result.truncated is True
