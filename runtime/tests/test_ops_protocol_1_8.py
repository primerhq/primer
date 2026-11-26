"""Tests for the protocol 1.8 runtime ops and exec's configured default deadline.

Review 2026-11-23: 5.5 (exec default timeout is the configured
``PRIMER_SUBPROCESS_TIMEOUT_SECONDS`` deadline, not a hard 60 s), 5.9
(``write_file_cas`` compare-and-swap under the write lock), 5.11
(``walk`` / ``search`` run in the runtime under entry/file/match/time caps)
and 5.12 (``read_file`` gains a ``limit`` so any window of a big file can
be paged in bounded frames).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import pathlib
import time

import pytest

from primer_runtime.exec import run_exec
from primer_runtime.locks import WorkspaceLockTable
from primer_runtime.ops import (
    OpError,
    _subprocess_timeout,
    read_file,
    search,
    walk,
    write_file_cas,
)
from primer_runtime.protocol import ErrorCode


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------------------
# read_file: limit (protocol 1.8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_limit_returns_first_n_bytes(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_bytes(b"0123456789")

    result = await read_file({"path": str(f), "limit": 4}, str(tmp_path))
    assert base64.b64decode(result["content_b64"]) == b"0123"


@pytest.mark.asyncio
async def test_read_file_offset_and_limit_window(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_bytes(b"0123456789")

    result = await read_file({"path": str(f), "offset": 3, "limit": 4}, str(tmp_path))
    assert base64.b64decode(result["content_b64"]) == b"3456"


@pytest.mark.asyncio
async def test_read_file_limit_past_eof_reads_nothing(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_bytes(b"0123456789")

    result = await read_file({"path": str(f), "offset": 8, "limit": 4}, str(tmp_path))
    assert base64.b64decode(result["content_b64"]) == b"89"


@pytest.mark.asyncio
async def test_read_file_limit_larger_than_file_returns_all(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_bytes(b"0123456789")

    result = await read_file({"path": str(f), "limit": 100}, str(tmp_path))
    assert base64.b64decode(result["content_b64"]) == b"0123456789"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, True, "4"])
async def test_read_file_invalid_limit_raises_eprotocol(tmp_path, bad):
    f = tmp_path / "hello.txt"
    f.write_bytes(b"0123456789")
    with pytest.raises(OpError) as exc_info:
        await read_file({"path": str(f), "limit": bad}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.EPROTOCOL


# ---------------------------------------------------------------------------
# walk (protocol 1.8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_walk_tree_entries(tmp_path):
    (tmp_path / "d1" / "d2").mkdir(parents=True)
    (tmp_path / "top.txt").write_bytes(b"1234567890")
    (tmp_path / "d1" / "a.txt").write_bytes(b"1234567890")
    (tmp_path / "d1" / "d2" / "b.bin").write_bytes(b"\x00\x01\x02")

    result = await walk({"path": str(tmp_path)}, str(tmp_path))
    assert result["truncated"] is False
    by_path = {e["path"]: e for e in result["entries"]}
    assert set(by_path) == {"top.txt", "d1", "d1/a.txt", "d1/d2", "d1/d2/b.bin"}
    assert by_path["d1"]["is_dir"] is True
    assert by_path["d1"]["size"] == 0
    assert by_path["top.txt"]["is_dir"] is False
    assert by_path["top.txt"]["size"] == 10
    assert by_path["d1/d2/b.bin"]["size"] == 3


@pytest.mark.asyncio
async def test_walk_max_entries_truncates(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_bytes(b"x")

    result = await walk(
        {"path": str(tmp_path), "max_entries": 3}, str(tmp_path)
    )
    assert result["truncated"] is True
    assert len(result["entries"]) == 3


@pytest.mark.asyncio
async def test_walk_missing_raises_enoent(tmp_path):
    with pytest.raises(OpError) as exc_info:
        await walk({"path": str(tmp_path / "nope")}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.ENOENT


@pytest.mark.asyncio
async def test_walk_file_raises_enotdir(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"x")
    with pytest.raises(OpError) as exc_info:
        await walk({"path": str(f)}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.ENOTDIR


@pytest.mark.asyncio
async def test_walk_symlinked_dir_reported_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.txt").write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(outside)

    result = await walk({"path": str(tmp_path)}, str(tmp_path))
    by_path = {e["path"]: e for e in result["entries"]}
    # The symlink itself is a non-directory entry...
    assert by_path["link"]["is_dir"] is False
    # ...and the directory it points at is NOT walked.
    assert "link/hidden.txt" not in by_path
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# search (protocol 1.8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_files_with_matches(tmp_path):
    (tmp_path / "a.txt").write_text("TODO a\n")
    (tmp_path / "b.txt").write_text("TODO b\n")
    (tmp_path / "c.txt").write_text("nothing\n")

    result = await search(
        {"path": str(tmp_path), "pattern": "TODO"}, str(tmp_path)
    )
    assert result["lines"] == ["a.txt", "b.txt"]
    assert result["match_count"] == 2
    assert result["file_count"] == 2
    assert result["truncated"] is False
    assert result["stop_reason"] is None


@pytest.mark.asyncio
async def test_search_count_mode_counts_matching_lines(tmp_path):
    (tmp_path / "a.txt").write_text("hit one\nother\nhit two\nhit three\n")

    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "output_mode": "count"},
        str(tmp_path),
    )
    assert result["lines"] == ["a.txt:3"]
    assert result["match_count"] == 3
    assert result["file_count"] == 1


@pytest.mark.asyncio
async def test_search_content_mode_merges_context(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo hit\nthree hit\nfour\nfive\n")

    result = await search(
        {
            "path": str(tmp_path),
            "pattern": "hit",
            "output_mode": "content",
            "context": 1,
        },
        str(tmp_path),
    )
    # The two context windows overlap on line 3: it appears once, and
    # the range stops at the merged context (line 5 is not included).
    assert result["lines"] == [
        "a.txt:1:one",
        "a.txt:2:two hit",
        "a.txt:3:three hit",
        "a.txt:4:four",
    ]


@pytest.mark.asyncio
async def test_search_multiline_escapes_newlines_in_snippets(tmp_path):
    (tmp_path / "a.txt").write_text("foo\nbar\n")

    result = await search(
        {
            "path": str(tmp_path),
            "pattern": "foo\\nbar",
            "output_mode": "content",
            "multiline": True,
        },
        str(tmp_path),
    )
    assert result["lines"] == ["a.txt::foo\\nbar"]
    assert result["match_count"] == 1


@pytest.mark.asyncio
async def test_search_glob_filter_and_case_insensitive(tmp_path):
    (tmp_path / "a.py").write_text("Needle\n")
    (tmp_path / "b.txt").write_text("needle\n")

    result = await search(
        {
            "path": str(tmp_path),
            "pattern": "needle",
            "glob": "*.py",
            "case_insensitive": True,
        },
        str(tmp_path),
    )
    assert result["lines"] == ["a.py"]


@pytest.mark.asyncio
async def test_search_max_files_cap(tmp_path):
    for i in range(4):
        (tmp_path / f"f{i}.txt").write_text("hit\n")

    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "max_files": 2},
        str(tmp_path),
    )
    assert result["truncated"] is True
    assert result["stop_reason"] == "file_cap"
    assert len(result["lines"]) == 2


@pytest.mark.asyncio
async def test_search_size_cap_skips_big_file_and_keeps_going(tmp_path):
    # Big file (over the cap) is skipped; the search still finds the small one.
    (tmp_path / "big.txt").write_text("hit " * 50)  # 200 bytes
    (tmp_path / "small.txt").write_text("hit\n")

    result = await search(
        {
            "path": str(tmp_path),
            "pattern": "hit",
            "max_file_bytes": 100,
        },
        str(tmp_path),
    )
    assert result["truncated"] is True
    assert result["stop_reason"] == "size_cap"
    assert result["lines"] == ["small.txt"]
    assert result["match_count"] == 1


@pytest.mark.asyncio
async def test_search_match_cap(tmp_path):
    (tmp_path / "a.txt").write_text("hit\n" * 5)

    result = await search(
        {
            "path": str(tmp_path),
            "pattern": "hit",
            "max_matches": 3,
            "output_mode": "count",
        },
        str(tmp_path),
    )
    assert result["truncated"] is True
    assert result["stop_reason"] == "match_cap"
    assert result["lines"] == ["a.txt:3"]
    assert result["match_count"] == 3


@pytest.mark.asyncio
async def test_search_skips_reserved_trees(tmp_path):
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state" / "m.txt").write_text("hit\n")
    (tmp_path / "a.txt").write_text("hit\n")

    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "skip": [".state"]},
        str(tmp_path),
    )
    assert result["lines"] == ["a.txt"]
    assert result["file_count"] == 1


@pytest.mark.asyncio
async def test_search_skips_binary_files(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\x00hit\x00")
    (tmp_path / "a.txt").write_text("hit\n")

    result = await search(
        {"path": str(tmp_path), "pattern": "hit"}, str(tmp_path)
    )
    assert result["lines"] == ["a.txt"]


@pytest.mark.asyncio
async def test_search_time_cap(tmp_path):
    # A file too big to scan inside the deadline: the deadline checked
    # between lines cuts the search short.
    (tmp_path / "big.log").write_bytes(b"line\n" * 200_000)

    result = await search(
        {"path": str(tmp_path), "pattern": "line", "timeout_s": 0.0001},
        str(tmp_path),
    )
    assert result["truncated"] is True
    assert result["stop_reason"] == "time_cap"


@pytest.mark.asyncio
async def test_search_invalid_pattern_raises_eprotocol(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(OpError) as exc_info:
        await search({"path": str(tmp_path), "pattern": "("}, str(tmp_path))
    assert exc_info.value.code == ErrorCode.EPROTOCOL


@pytest.mark.asyncio
async def test_search_missing_path_raises_enoent(tmp_path):
    with pytest.raises(OpError) as exc_info:
        await search(
            {"path": str(tmp_path / "nope"), "pattern": "x"}, str(tmp_path)
        )
    assert exc_info.value.code == ErrorCode.ENOENT


# ---------------------------------------------------------------------------
# write_file_cas (protocol 1.8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_file_cas_applies_on_match(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"original")
    locks = WorkspaceLockTable()

    result = await write_file_cas(
        {
            "path": str(f),
            "expected_sha256": sha(b"original"),
            "content_b64": b64(b"updated"),
        },
        str(tmp_path),
        locks,
    )
    assert result["applied"] is True
    assert f.read_bytes() == b"updated"


@pytest.mark.asyncio
async def test_write_file_cas_mismatch_leaves_file_untouched(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"original")
    locks = WorkspaceLockTable()

    result = await write_file_cas(
        {
            "path": str(f),
            "expected_sha256": sha(b"something else"),
            "content_b64": b64(b"updated"),
        },
        str(tmp_path),
        locks,
    )
    assert result["applied"] is False
    assert f.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_write_file_cas_missing_file_raises_enoent(tmp_path):
    locks = WorkspaceLockTable()
    with pytest.raises(OpError) as exc_info:
        await write_file_cas(
            {
                "path": str(tmp_path / "missing.txt"),
                "expected_sha256": sha(b""),
                "content_b64": b64(b"x"),
            },
            str(tmp_path),
            locks,
        )
    assert exc_info.value.code == ErrorCode.ENOENT


@pytest.mark.asyncio
async def test_write_file_cas_invalid_base64_raises_eprotocol(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"original")
    locks = WorkspaceLockTable()
    with pytest.raises(OpError) as exc_info:
        await write_file_cas(
            {
                "path": str(f),
                "expected_sha256": sha(b"original"),
                "content_b64": "!!!not-base64!!!",
            },
            str(tmp_path),
            locks,
        )
    assert exc_info.value.code == ErrorCode.EPROTOCOL


@pytest.mark.asyncio
async def test_write_file_cas_rejects_a_malformed_digest(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"original")
    with pytest.raises(OpError) as exc_info:
        await write_file_cas(
            {"path": str(f), "expected_sha256": "abc", "content_b64": b64(b"x")},
            str(tmp_path),
            WorkspaceLockTable(),
        )
    assert exc_info.value.code == ErrorCode.EPROTOCOL


@pytest.mark.asyncio
async def test_write_file_cas_hashes_a_large_file_without_the_old_bytes(tmp_path):
    # 8 MiB: the compare streams through the digest (the old op accumulated
    # `current += chunk` and was sent the whole old content).
    f = tmp_path / "big.bin"
    body = b"0123456789abcdef" * (512 * 1024)
    f.write_bytes(body)
    result = await write_file_cas(
        {"path": str(f), "expected_sha256": sha(body), "content_b64": b64(b"small")},
        str(tmp_path),
        WorkspaceLockTable(),
    )
    assert result["applied"] is True
    assert f.read_bytes() == b"small"


# ---------------------------------------------------------------------------
# Fix round 1: caps across the whole walk, breadth-first order, special files,
# workspace-relative paths, byte limits, regex timeout without a frozen loop
# ---------------------------------------------------------------------------


def _nested_tree(root: pathlib.Path, dirs: int = 30) -> None:
    """``dirs`` one-file directories at depth 1, each holding a deeper file, plus top files."""
    for i in range(dirs):
        d = root / f"d{i:03d}"
        (d / "deep").mkdir(parents=True)
        (d / "f.txt").write_text("hit\n")
        (d / "deep" / "g.txt").write_text("hit\n")
    (root / "top.txt").write_text("hit\n")


@pytest.mark.asyncio
async def test_walk_max_entries_is_enforced_across_nested_directories(tmp_path):
    _nested_tree(tmp_path)
    result = await walk({"path": str(tmp_path), "max_entries": 10}, str(tmp_path))
    assert result["truncated"] is True
    assert len(result["entries"]) == 10


@pytest.mark.asyncio
async def test_walk_is_breadth_first_in_sorted_order(tmp_path):
    (tmp_path / "a" / "deep").mkdir(parents=True)
    (tmp_path / "a" / "deep" / "x.txt").write_text("x")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "y.txt").write_text("y")
    (tmp_path / "z.txt").write_text("z")
    result = await walk({"path": str(tmp_path)}, str(tmp_path))
    assert [e["path"] for e in result["entries"]] == [
        "a", "b", "z.txt", "a/deep", "b/y.txt", "a/deep/x.txt"
    ]


@pytest.mark.asyncio
async def test_search_max_files_is_enforced_across_nested_directories(tmp_path):
    _nested_tree(tmp_path)
    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "max_files": 10, "output_mode": "count"},
        str(tmp_path),
    )
    assert result["truncated"] is True
    assert result["stop_reason"] == "file_cap"
    assert len(result["lines"]) == 10
    # breadth-first: the shallow files come before any deep one
    assert result["lines"][0] == "top.txt:1"
    assert not any("deep" in line for line in result["lines"])


@pytest.mark.asyncio
async def test_search_skips_a_fifo_instead_of_blocking(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "a.txt").write_text("hit\n")
    result = await asyncio.wait_for(
        search({"path": str(tmp_path), "pattern": "hit"}, str(tmp_path)), timeout=10
    )
    assert result["lines"] == ["a.txt"]
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_search_paths_are_relative_to_the_workspace_root(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("hit\n")
    (tmp_path / "src" / "pkg" / "b.py").write_text("one\nhit\n")
    (tmp_path / "other.py").write_text("hit\n")

    result = await search(
        {"path": str(tmp_path / "src"), "pattern": "hit", "output_mode": "content"},
        str(tmp_path),
    )
    assert result["lines"] == ["src/a.py:1:hit", "src/pkg/b.py:2:hit"]
    # a glob still filters on the path relative to the SEARCHED directory
    result = await search(
        {"path": str(tmp_path / "src"), "pattern": "hit", "glob": "pkg/*.py"},
        str(tmp_path),
    )
    assert result["lines"] == ["src/pkg/b.py"]


@pytest.mark.asyncio
async def test_search_reserved_tree_skip_is_relative_to_the_workspace_root(tmp_path):
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state" / "m.txt").write_text("hit\n")
    (tmp_path / "sub" / ".state").mkdir(parents=True)
    (tmp_path / "sub" / ".state" / "k.txt").write_text("hit\n")
    (tmp_path / "sub" / "a.txt").write_text("hit\n")

    # Grepping sub/ must NOT skip sub/.state (only the workspace's own .state is reserved)...
    result = await search(
        {"path": str(tmp_path / "sub"), "pattern": "hit", "skip": [".state"]}, str(tmp_path)
    )
    assert sorted(result["lines"]) == ["sub/.state/k.txt", "sub/a.txt"]
    # ...while the workspace root still skips its .state.
    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "skip": [".state"]}, str(tmp_path)
    )
    assert "sub/a.txt" in result["lines"] and ".state/m.txt" not in result["lines"]


@pytest.mark.asyncio
async def test_search_head_limit_cuts_lines_but_keeps_counting(tmp_path):
    (tmp_path / "a.txt").write_text("hit\n" * 20)
    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "output_mode": "content", "head_limit": 5},
        str(tmp_path),
    )
    assert len(result["lines"]) == 5
    assert result["truncated"] is True
    assert result["match_count"] == 20


@pytest.mark.asyncio
async def test_search_output_byte_cap_bounds_the_response(tmp_path):
    # Minified-style long lines: content mode would otherwise echo every one.
    for i in range(10):
        (tmp_path / f"m{i}.js").write_text("hit " + "x" * 5000 + "\n")
    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "output_mode": "content", "max_output_bytes": 12000},
        str(tmp_path),
    )
    assert sum(len(line.encode()) + 1 for line in result["lines"]) <= 12000
    assert 1 <= len(result["lines"]) < 10
    assert "output_cap" in result["stop_reasons"]
    assert result["truncated"] is True
    assert result["match_count"] == 10


@pytest.mark.asyncio
async def test_search_size_cap_no_longer_hides_the_match_cap(tmp_path):
    (tmp_path / "a_big.txt").write_text("hit " * 100)
    (tmp_path / "b.txt").write_text("hit\n" * 6)
    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "max_file_bytes": 100, "max_matches": 3,
         "output_mode": "count"},
        str(tmp_path),
    )
    assert result["stop_reasons"] == ["size_cap", "match_cap"]
    assert result["skipped_large"] == 1
    assert result["match_count"] == 3


@pytest.mark.asyncio
async def test_search_catastrophic_pattern_times_out_without_freezing_the_event_loop(
    tmp_path, monkeypatch
):
    # stdlib `re` runs this on a 40 character line for minutes with the GIL held; the
    # `regex` library under timeout= (GIL released) stops it and the loop keeps running.
    monkeypatch.setattr("primer_runtime.ops._SEARCH_LINE_TIMEOUT_S", 0.5)
    (tmp_path / "evil.txt").write_text("a" * 40 + "b\n")
    (tmp_path / "ok.txt").write_text("fine\n")

    ticks = 0
    stop = False

    async def heartbeat() -> None:
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0.02)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    concurrent_op = asyncio.create_task(read_file({"path": str(tmp_path / "ok.txt")}, str(tmp_path)))
    started = time.monotonic()
    result = await search({"path": str(tmp_path), "pattern": "(a|aa)+$"}, str(tmp_path))
    elapsed = time.monotonic() - started
    other = await concurrent_op  # another op completed during the search
    stop = True
    await beat

    assert elapsed < 5
    assert result["truncated"] is True
    assert result["stop_reason"] == "regex_timeout"
    assert ticks >= elapsed / 0.02 * 0.5  # the loop ran for (most of) the search
    assert base64.b64decode(other["content_b64"]) == b"fine\n"


@pytest.mark.asyncio
async def test_search_regex_only_syntax_compiles(tmp_path):
    (tmp_path / "a.txt").write_text("TODO: x\n")
    result = await search({"path": str(tmp_path), "pattern": "(?|TODO)"}, str(tmp_path))
    assert result["lines"] == ["a.txt"]


@pytest.mark.asyncio
async def test_search_file_cap_not_flagged_when_exactly_max_files_fill_the_tree(tmp_path):
    # Exactly max_files exist and the walk exhausts the tree on the last one:
    # nothing was dropped, so file_cap must not fire (it used to mean "the cap
    # was reached", not "the walk was cut short", and flagged here).
    for i in range(10):
        (tmp_path / f"f{i:02d}.py").write_text("hit\n")
    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "max_files": 10, "output_mode": "count"},
        str(tmp_path),
    )
    assert result["stop_reasons"] == []
    assert result["truncated"] is False
    assert len(result["lines"]) == 10


@pytest.mark.asyncio
async def test_search_file_cap_flagged_when_a_file_is_actually_dropped(tmp_path):
    # One more file than the cap: the 11th is dropped, so file_cap fires.
    for i in range(11):
        (tmp_path / f"f{i:02d}.py").write_text("hit\n")
    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "max_files": 10, "output_mode": "count"},
        str(tmp_path),
    )
    assert result["stop_reason"] == "file_cap"
    assert result["truncated"] is True
    assert len(result["lines"]) == 10


@pytest.mark.asyncio
async def test_search_file_cap_flagged_when_unlisted_directories_remain(tmp_path):
    # The cap fills in the top directory while a subdirectory with a matching
    # file is still queued: that file is dropped, so file_cap fires.
    for i in range(10):
        (tmp_path / f"f{i:02d}.py").write_text("hit\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.py").write_text("hit\n")
    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "max_files": 10},
        str(tmp_path),
    )
    assert "file_cap" in result["stop_reasons"]
    assert len(result["lines"]) == 10


@pytest.mark.asyncio
async def test_search_file_cap_counts_only_files_that_pass_the_glob(tmp_path):
    # 30 data files at the top and one .py file one level down: a cap of 10 files of ANY
    # name used to be spent on the .txt files, so a *.py search found nothing.
    for i in range(30):
        (tmp_path / f"data{i:02d}.txt").write_text("hit\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.py").write_text("hit\n")

    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "glob": "*.py", "max_files": 10},
        str(tmp_path),
    )
    assert result["lines"] == ["sub/x.py"]
    assert result["truncated"] is False
    assert result["stop_reasons"] == []


@pytest.mark.asyncio
async def test_search_file_cap_still_fires_for_matching_files(tmp_path):
    for i in range(30):
        (tmp_path / f"m{i:02d}.py").write_text("hit\n")
        (tmp_path / f"d{i:02d}.txt").write_text("hit\n")
    result = await search(
        {"path": str(tmp_path), "pattern": "hit", "glob": "*.py", "max_files": 10,
         "output_mode": "count"},
        str(tmp_path),
    )
    assert result["stop_reason"] == "file_cap"
    assert len(result["lines"]) == 10
    assert all(line.endswith(".py:1") for line in result["lines"])


# ---------------------------------------------------------------------------
# exec: the default timeout is the configured subprocess deadline (5.5)
# ---------------------------------------------------------------------------


async def _collect_exec(
    req_id: int, args: dict, workspace_root: str
) -> tuple[list[dict], int, bool]:
    """Run exec and collect (stream_events, exit_code, timed_out)."""
    stream_events: list[dict] = []
    exit_code = -99
    timed_out = False

    async for event in run_exec(req_id, args, workspace_root, WorkspaceLockTable()):
        if event.event == "exit":
            assert event.data is not None
            exit_code = event.data["code"]
            timed_out = event.data.get("timed_out", False)
        else:
            assert event.data is not None
            stream_events.append(
                {"event": event.event, "data_b64": event.data["data_b64"]}
            )

    return stream_events, exit_code, timed_out


@pytest.mark.asyncio
async def test_subprocess_timeout_default_is_120_seconds(monkeypatch):
    """The runtime default is the configured 120 s deadline (not the old
    hard 60 s), so a default-timeout exec and init commands get the same
    bound the platform's AppConfig uses."""
    monkeypatch.delenv("PRIMER_SUBPROCESS_TIMEOUT_SECONDS", raising=False)
    assert _subprocess_timeout() == 120.0


@pytest.mark.asyncio
async def test_exec_default_timeout_uses_the_configured_deadline(
    tmp_path, monkeypatch
):
    """Without an explicit timeout_s, exec honours
    PRIMER_SUBPROCESS_TIMEOUT_SECONDS: a sleep past it is killed and the
    exit event carries timed_out (the client turns that into a
    TimeoutError, review 2026-11-23, 5.5)."""
    monkeypatch.setenv("PRIMER_SUBPROCESS_TIMEOUT_SECONDS", "1")
    events, code, timed_out = await _collect_exec(
        2, {"cmd": ["sleep", "3"]}, str(tmp_path)
    )
    assert timed_out is True
    assert code == -1


@pytest.mark.asyncio
async def test_exec_explicit_timeout_still_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMER_SUBPROCESS_TIMEOUT_SECONDS", raising=False)
    events, code, timed_out = await _collect_exec(
        3, {"cmd": ["sleep", "5"], "timeout_s": 1}, str(tmp_path)
    )
    assert timed_out is True
    assert code == -1
