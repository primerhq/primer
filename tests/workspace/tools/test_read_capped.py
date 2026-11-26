"""Review 2026-11-23 finding 5.12 (local half), fix round 1: ``read``
serves ANY window of ANY file with bounded memory.

``read`` used to load the WHOLE file into the API process. It now
iterates the file's lines from the start (never loading the whole
file), skips to ``offset``, collects up to ``limit`` lines, and stops
collecting -- with a truncation note -- once the returned text would
exceed ``TOOL_RESULT_CAP_BYTES`` (50 KiB, the same cap the runtime's
outer truncation applies to any tool result). Memory stays bounded by
the cap; time is proportional to the offset.

Fix round 2: the line walk reads the file in bounded binary chunks and
never buffers one line past the cap -- a newline-less line longer than
the cap is served cut at its head with a note (round 1 read such a line
whole into memory before the cap check ran) -- and the cap is measured
in ENCODED bytes, so non-ASCII text cannot return 4x the cap.

Fix round 3: the chunk state machine is replaced by a text-mode reader
(`readline` bounded to the cap in characters, universal newlines,
``errors="replace"``), which fixes two round-2 breakages -- the early
stop for an over-budget line served a third line past ``limit`` with
``had_more`` False, and binary mode left a trailing ``\\r`` on every
CRLF line (and stopped splitting on a lone CR) -- and bounds the
encoded result for invalid UTF-8 (a line of replaced bytes returns at
most the cap plus the notes, not ~3x).
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.workspace.local.tools import Read, ReadArgs
from primer.workspace.local.tools._common import TOOL_RESULT_CAP_BYTES


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(session=SimpleNamespace(mark_read=lambda p: None))


async def test_a_window_near_the_end_of_a_multi_mib_file_returns_the_right_lines(
    tmp_path: Path
) -> None:
    """Several MiB on disk; a window near the END returns exactly those
    lines (the file is never loaded whole to answer it)."""
    n = 30_000
    path = tmp_path / "big.txt"
    path.write_text(
        "\n".join(f"line-{i:06d} " + "x" * 100 for i in range(n)) + "\n"
    )
    assert path.stat().st_size > 3 * 1024 * 1024

    result = await Read(tmp_path).execute(
        ReadArgs(path="big.txt", offset=n - 3, limit=3), _ctx()
    )
    expected = "\n".join(
        f"{n - 3 + i + 1:>6}\u2192line-{n - 3 + i:06d} " + "x" * 100
        for i in range(3)
    )
    assert result.output == expected
    assert result.truncated is True  # offset > 0


async def test_a_window_whose_lines_exceed_the_cap_is_cut_with_a_note(
    tmp_path: Path
) -> None:
    """A window bigger than the cap is cut, says so, and stays bounded."""
    path = tmp_path / "big.txt"
    # 200 lines of ~4 KiB each: ~800 KiB, well past the 50 KiB cap.
    path.write_text(
        "\n".join(f"chunk-{i:04d} " + "y" * 4000 for i in range(200)) + "\n"
    )

    result = await Read(tmp_path).execute(
        ReadArgs(path="big.txt", limit=None), _ctx()
    )
    out = result.output
    assert result.truncated is True
    assert "window cut" in out
    assert str(TOOL_RESULT_CAP_BYTES) in out
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + 256
    assert out.startswith("     1\u2192chunk-0000")


async def test_a_small_file_reads_exactly_as_before(tmp_path: Path) -> None:
    """Under the cap and offset 0: the output is unchanged from the
    pre-fix behaviour and nothing is flagged or noted."""
    path = tmp_path / "small.txt"
    path.write_text("a\nb\nc\n")

    result = await Read(tmp_path).execute(ReadArgs(path="small.txt"), _ctx())
    assert result.output == "     1\u2192a\n     2\u2192b\n     3\u2192c"
    assert result.truncated is False
    assert "cut" not in result.output


async def test_the_pagination_flag_is_preserved(tmp_path: Path) -> None:
    """The pre-existing pagination flag (a window that does not cover the
    rest) is independent of the byte cap: a window covering exactly the
    whole file is not truncated, a shorter one is."""
    path = tmp_path / "small.txt"
    path.write_text("a\nb\nc\nd\ne\n")

    result = await Read(tmp_path).execute(
        ReadArgs(path="small.txt", offset=0, limit=2), _ctx()
    )
    assert result.output == "     1\u2192a\n     2\u2192b"
    assert result.truncated is True  # more lines past the window

    result = await Read(tmp_path).execute(
        ReadArgs(path="small.txt", offset=0, limit=5), _ctx()
    )
    assert result.output == "     1\u2192a\n     2\u2192b\n     3\u2192c\n     4\u2192d\n     5\u2192e"
    assert result.truncated is False  # the window covers the whole file


async def test_a_window_past_eof_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "small.txt"
    path.write_text("a\nb\n")

    result = await Read(tmp_path).execute(
        ReadArgs(path="small.txt", offset=100), _ctx()
    )
    assert result.output == ""
    assert result.truncated is True  # offset > 0, as before the fix


# ---------------------------------------------------------------------------
# Fix round 2: bounded reading of a single huge line, encoded-byte cap.
# ---------------------------------------------------------------------------


async def test_a_single_huge_line_returns_its_head_cut_with_a_note(
    tmp_path: Path,
) -> None:
    """Round 2: a 20 MiB file that is ONE line (no newline at all) must
    return quickly with the HEAD of that line and a note that the line
    was cut -- round 1 read the whole line into memory before the cap
    check ran and returned empty plus '0 lines shown'."""
    path = tmp_path / "huge-oneline.bin"
    path.write_bytes(b"z" * (20 * 1024 * 1024))
    assert path.stat().st_size == 20 * 1024 * 1024

    t0 = time.monotonic()
    result = await Read(tmp_path).execute(ReadArgs(path="huge-oneline.bin"), _ctx())
    elapsed = time.monotonic() - t0

    out = result.output
    assert elapsed < 10.0, f"reading one 20 MiB line took {elapsed:.1f} s"
    assert result.truncated is True
    assert out.startswith("     1\u2192z"), (
        f"the head of the line must be visible, got: {out[:80]!r}"
    )
    # The cut is NAMED, not just implied by the truncation flag.
    assert "last line cut" in out
    # The result stays within the cap plus the line-number prefix and the
    # two notes.
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + 256
    # The numbered head fills the cap exactly (the 9-byte prefix counts
    # against it): nothing more of the 20 MiB line made it into the result.
    head = out.split("\n", 1)[0]
    assert head.split("\u2192", 1)[1] == "z" * (TOOL_RESULT_CAP_BYTES - 9)


async def test_a_long_line_in_the_middle_does_not_hide_later_lines(
    tmp_path: Path,
) -> None:
    """Round 2: a 2 MiB line in the middle of a normal file must not
    swallow the lines after it -- a later window (offset past the big
    line) still returns exactly its own lines, and a window starting AT
    the big line serves its head, cut and named."""
    path = tmp_path / "mid.txt"
    head_lines = [f"s{i:05d}" for i in range(1000)]
    head_lines.append("BIGLINE" + "m" * (2 * 1024 * 1024))
    head_lines += [f"t{i:05d}" for i in range(1000)]
    path.write_text("\n".join(head_lines) + "\n")

    # Window right after the big line: 1-based 1003..1007.
    result = await Read(tmp_path).execute(
        ReadArgs(path="mid.txt", offset=1002, limit=5), _ctx()
    )
    expected = "\n".join(f"{i:>6}\u2192{t}" for i, t in enumerate(
        (f"t{i:05d}" for i in range(1, 6)), start=1003
    ))
    assert result.output == expected
    assert result.truncated is True  # the file continues past the window

    # Window starting AT the big line: its head is served, cut, named.
    result = await Read(tmp_path).execute(
        ReadArgs(path="mid.txt", offset=1000, limit=3), _ctx()
    )
    out = result.output
    assert out.startswith("  1001\u2192BIGLINE")
    assert "last line cut" in out
    assert result.truncated is True


async def test_the_cap_is_measured_in_encoded_bytes(tmp_path: Path) -> None:
    """Round 2: the cap is measured on the ENCODED result, so non-ASCII
    text (2 bytes per char in UTF-8) cannot return ~4x the cap the way
    character counting did."""
    path = tmp_path / "accented.txt"
    # 100 lines of 6000 e-accents: 12000 ENCODED bytes per line, but only
    # ~6000 characters.
    path.write_text("\n".join("\u00e9" * 6000 for _ in range(100)) + "\n")

    result = await Read(tmp_path).execute(
        ReadArgs(path="accented.txt", limit=None), _ctx()
    )
    out = result.output
    assert result.truncated is True
    assert "window cut" in out
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + 256


# ---------------------------------------------------------------------------
# Fix round 3: limit honoured on the over-budget stop, CRLF / lone CR,
# invalid-UTF-8 encoded bound.
# ---------------------------------------------------------------------------


async def test_limit_is_honoured_when_the_window_ends_on_a_huge_line(
    tmp_path: Path,
) -> None:
    """Round 3 probe: two short lines plus a 1 MiB newline-less line,
    ``limit=2`` -- the reader must return EXACTLY the two lines and say
    there is more (round 2's early stop for the over-budget line served
    a third line and left had_more False)."""
    path = tmp_path / "limit-huge.txt"
    path.write_bytes(b"a\nb\n" + b"z" * (1024 * 1024))

    result = await Read(tmp_path).execute(
        ReadArgs(path="limit-huge.txt", limit=2), _ctx()
    )
    assert result.output == "     1\u2192a\n     2\u2192b"
    assert result.truncated is True  # the 1 MiB line continues past the window


async def test_crlf_lines_carry_no_trailing_cr_and_lone_cr_splits(
    tmp_path: Path,
) -> None:
    """Round 3 probe: binary mode does not translate line endings, so
    round 2 left a trailing ``\\r`` on every CRLF line and stopped
    splitting on a lone CR; the original reader (text mode) treated
    CRLF and a lone CR as line breaks."""
    crlf = tmp_path / "crlf.txt"
    crlf.write_bytes(b"a\r\nb\r\nc\r\n")

    result = await Read(tmp_path).execute(ReadArgs(path="crlf.txt"), _ctx())
    assert result.output == "     1\u2192a\n     2\u2192b\n     3\u2192c"
    assert result.truncated is False
    assert "\r" not in result.output

    lone = tmp_path / "lone-cr.txt"
    lone.write_bytes(b"a\rb\nc\n")

    result = await Read(tmp_path).execute(ReadArgs(path="lone-cr.txt"), _ctx())
    assert result.output == "     1\u2192a\n     2\u2192b\n     3\u2192c"
    assert "\r" not in result.output


async def test_invalid_utf8_stays_within_the_cap_plus_notes(
    tmp_path: Path,
) -> None:
    """Round 3 minor: 60,000 bytes of ``\\xff`` decode to 60,000
    replaced characters (3 encoded bytes each) -- round 2's line budget
    was in RAW bytes, so such a line was served at ~153,609 encoded
    bytes. The new bound is in ENCODED bytes: the result stays within
    the cap plus the number prefix and the notes."""
    path = tmp_path / "invalid-utf8.bin"
    path.write_bytes(b"\xff" * 60_000)

    result = await Read(tmp_path).execute(ReadArgs(path="invalid-utf8.bin"), _ctx())
    out = result.output
    assert result.truncated is True
    assert "last line cut" in out
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + 256
    # The head is the line's head in ENCODED bytes, cut on a character
    # boundary (a replaced character is exactly 3 encoded bytes).
    head = out.split("\n", 1)[0].split("\u2192", 1)[1]
    assert 0 < len(head.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES


# ---------------------------------------------------------------------------
# Fix round 4: an over-cap line AFTER other lines is cut to the REMAINING
# room, not to the full cap.
# ---------------------------------------------------------------------------

_SLACK = 300  # the two notes plus rounding


async def test_a_huge_line_after_other_lines_stays_within_the_cap(
    tmp_path: Path,
) -> None:
    """Round 4: 40,000 bytes of ``a``, a newline, then a 1 MiB ``b`` line
    returned ~91 KB (the huge line was cut to the FULL cap, ignoring what
    the first line already used)."""
    path = tmp_path / "after.txt"
    path.write_bytes(b"a" * 40_000 + b"\n" + b"b" * (1024 * 1024))

    result = await Read(tmp_path).execute(
        ReadArgs(path="after.txt", limit=None), _ctx()
    )
    out = result.output
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + _SLACK
    assert result.truncated is True
    assert "window cut" in out and "last line cut" in out
    assert out.startswith("     1\u2192" + "a" * 40_000 + "\n     2\u2192b")


async def test_a_huge_non_ascii_line_after_other_lines_stays_within_the_cap(
    tmp_path: Path,
) -> None:
    """Round 4: the same with 2-byte characters."""
    path = tmp_path / "after-utf8.txt"
    path.write_text("\u00e9" * 20_000 + "\n" + "\u00e8" * (512 * 1024))

    result = await Read(tmp_path).execute(
        ReadArgs(path="after-utf8.txt", limit=None), _ctx()
    )
    out = result.output
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + _SLACK
    assert result.truncated is True
    assert "last line cut" in out
    assert "     2\u2192\u00e8" in out


async def test_a_huge_third_line_with_a_small_limit_stays_within_the_cap(
    tmp_path: Path,
) -> None:
    """Round 4: ``limit=3`` after two short lines whose third line is
    huge returned ~51,231 bytes."""
    path = tmp_path / "third.txt"
    path.write_bytes(b"a" * 30_000 + b"\n" + b"b" * 10 + b"\n" + b"c" * (1024 * 1024))

    result = await Read(tmp_path).execute(
        ReadArgs(path="third.txt", limit=3), _ctx()
    )
    out = result.output
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + _SLACK
    assert result.truncated is True
    assert "last line cut" in out
    assert "     3\u2192c" in out


async def test_a_first_line_only_huge_file_still_returns_its_head(
    tmp_path: Path,
) -> None:
    """Round 4: the first line has the whole cap as room, so its head is
    about the cap (the number prefix counts against it)."""
    path = tmp_path / "first.txt"
    path.write_bytes(b"q" * (1024 * 1024))

    result = await Read(tmp_path).execute(ReadArgs(path="first.txt"), _ctx())
    out = result.output
    head = out.split("\n", 1)[0]
    assert len(head.encode("utf-8")) == TOOL_RESULT_CAP_BYTES
    assert "last line cut" in out
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + _SLACK
