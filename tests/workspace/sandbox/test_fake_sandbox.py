"""Tests for FakeSandbox itself.

Validates that the test seam behaves like a real sandbox to the
contract the :class:`Sandbox` ABC promises.
"""
from __future__ import annotations

import asyncio
import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from primer.workspace.sandbox.fake import FakeSandbox


@pytest.mark.asyncio
async def test_exec_echo(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    res = await sb.exec("echo hi")
    assert res.exit_code == 0
    assert res.stdout.strip() == "hi"
    assert res.stderr == ""


@pytest.mark.asyncio
async def test_write_and_read(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/hello.txt", b"world")
    body = await sb.read_file("/workspace/hello.txt")
    assert body == b"world"


@pytest.mark.asyncio
async def test_list_dir(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/a.txt", b"1")
    await sb.write_file("/workspace/b.txt", b"22")
    entries = await sb.list_dir("/workspace")
    names = sorted(e.path for e in entries)
    assert names == ["a.txt", "b.txt"]


@pytest.mark.asyncio
async def test_stat_missing_returns_none(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    assert await sb.stat("/workspace/missing") is None


@pytest.mark.asyncio
async def test_stat_file(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"hello")
    info = await sb.stat("/workspace/x.txt")
    assert info is not None
    assert info.kind == "file"
    assert info.size_bytes == 5


@pytest.mark.asyncio
async def test_stat_fifo_preserves_file_type_bits(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "events")
    sb = FakeSandbox(root=tmp_path)

    info = await sb.stat("/workspace/events")

    assert info is not None
    assert info.kind == "file"
    assert stat.S_ISFIFO(info.mode)


@pytest.mark.asyncio
async def test_delete(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"hi")
    await sb.delete("/workspace/x.txt")
    assert await sb.stat("/workspace/x.txt") is None


import sys

_SLEEP_5_ARGV = [sys.executable, "-c", "import time; time.sleep(5)"]


@pytest.mark.asyncio
async def test_exec_timeout(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    with pytest.raises(TimeoutError):
        await sb.exec(_SLEEP_5_ARGV, timeout_seconds=0.2)


@pytest.mark.asyncio
async def test_exec_abort(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    abort = asyncio.Event()

    async def trigger() -> None:
        await asyncio.sleep(0.2)
        abort.set()

    asyncio.create_task(trigger())
    res = await sb.exec(_SLEEP_5_ARGV, abort=abort, timeout_seconds=5.0)
    assert res.exit_code != 0  # killed


@pytest.mark.asyncio
async def test_archive_streams_tar(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"hi")
    chunks: list[bytes] = []
    async for c in sb.archive(["/workspace"]):
        chunks.append(c)
    buf = io.BytesIO(b"".join(chunks))
    with tarfile.open(fileobj=buf, mode="r") as tf:
        names = tf.getnames()
    # The archive includes the directory entry plus the file inside.
    assert any(n.endswith("x.txt") for n in names)


@pytest.mark.asyncio
async def test_inspect_running(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    info = await sb.inspect()
    assert info.state == "running"


@pytest.mark.asyncio
async def test_stop_then_exec_fails(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.stop()
    with pytest.raises(RuntimeError):
        await sb.exec("echo hi")


@pytest.mark.asyncio
async def test_stop_then_inspect_says_stopped(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.stop()
    info = await sb.inspect()
    assert info.state == "stopped"


@pytest.mark.asyncio
async def test_write_with_mode(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.sh", b"echo hi", mode=0o755)
    info = await sb.stat("/workspace/x.sh")
    assert info is not None
    # Mode setting is best-effort; just confirm we don't crash.


# ---------------------------------------------------------------------------
# search: the host twin of the runtime's search op (protocol 1.8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_files_with_matches_uses_workspace_relative_paths(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/src/a.py", b"x = 1  # TODO\n")
    await sb.write_file("/workspace/src/deep/b.py", b"# TODO here\n")
    await sb.write_file("/workspace/c.py", b"nothing matches\n")
    result = await sb.search("/workspace", "TODO")
    assert result["lines"] == ["src/a.py", "src/deep/b.py"]
    assert result["match_count"] == 2
    assert result["file_count"] == 2
    assert result["truncated"] is False
    assert result["stop_reason"] is None
    assert result["stop_reasons"] == []
    assert result["skipped_large"] == 0


@pytest.mark.asyncio
async def test_search_count_mode_counts_matching_lines(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/a.txt", b"hit one\nhit two\nmiss\n")
    result = await sb.search("/workspace", "hit", output_mode="count")
    assert result["lines"] == ["a.txt:2"]
    assert result["match_count"] == 2


@pytest.mark.asyncio
async def test_search_content_mode_merges_context(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/a.txt", b"l0\nl1\nHIT\nl3\nl4\n")
    result = await sb.search("/workspace", "HIT", output_mode="content", context=1)
    # The hit line plus one context line each side, merged, one entry per line.
    assert result["lines"] == ["a.txt:2:l1", "a.txt:3:HIT", "a.txt:4:l3"]


@pytest.mark.asyncio
async def test_search_multiline_content_escapes_newlines(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/a.txt", b"start\nhit1\nhit2\nend\n")
    result = await sb.search(
        "/workspace", r"hit1\nhit2", output_mode="content", multiline=True
    )
    assert result["lines"] == [r"a.txt::hit1\nhit2"]
    assert result["match_count"] == 1


@pytest.mark.asyncio
async def test_search_glob_filter_runs_before_the_file_cap_counts(tmp_path: Path) -> None:
    # The runtime probe mirrored host-side: 30 data files at the top and one
    # .py file one level down; a cap of 10 of ANY name used to be spent on
    # the .txt files, so a *.py search found nothing.
    sb = FakeSandbox(root=tmp_path)
    for i in range(30):
        await sb.write_file(f"/workspace/data{i:02d}.txt", b"hit\n")
    await sb.write_file("/workspace/sub/x.py", b"hit\n")
    result = await sb.search(
        "/workspace", "hit", glob="*.py", max_files=10, output_mode="files_with_matches"
    )
    assert result["lines"] == ["sub/x.py"]
    assert result["truncated"] is False
    assert result["stop_reasons"] == []


@pytest.mark.asyncio
async def test_search_file_cap_not_flagged_when_exactly_max_files_fill_the_tree(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    for i in range(10):
        await sb.write_file(f"/workspace/f{i:02d}.py", b"hit\n")
    result = await sb.search("/workspace", "hit", max_files=10)
    assert result["stop_reasons"] == []
    assert result["truncated"] is False
    assert len(result["lines"]) == 10


@pytest.mark.asyncio
async def test_search_file_cap_flagged_when_a_file_is_actually_dropped(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    for i in range(11):
        await sb.write_file(f"/workspace/f{i:02d}.py", b"hit\n")
    result = await sb.search("/workspace", "hit", max_files=10)
    assert result["stop_reason"] == "file_cap"
    assert result["truncated"] is True
    assert len(result["lines"]) == 10


@pytest.mark.asyncio
async def test_search_size_cap_skips_the_big_file_and_keeps_searching(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/big.txt", b"hit " * 100)
    await sb.write_file("/workspace/small.txt", b"hit\n")
    result = await sb.search(
        "/workspace", "hit", max_file_bytes=100, output_mode="count"
    )
    assert result["lines"] == ["small.txt:1"]
    assert result["stop_reasons"] == ["size_cap"]
    assert result["skipped_large"] == 1
    assert result["match_count"] == 1


@pytest.mark.asyncio
async def test_search_match_cap_stops_counting(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    for i in range(4):
        await sb.write_file(f"/workspace/f{i}.txt", b"hit\nhit\n")
    result = await sb.search("/workspace", "hit", max_matches=3)
    assert result["stop_reason"] == "match_cap"
    assert result["match_count"] == 3
    # files_with_matches is one line per FILE: 3 matches across 2 files.
    assert result["lines"] == ["f0.txt", "f1.txt"]
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_search_head_limit_cuts_lines_but_not_the_counts(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    for i in range(4):
        await sb.write_file(f"/workspace/f{i}.txt", b"hit\n")
    result = await sb.search("/workspace", "hit", head_limit=2)
    assert len(result["lines"]) == 2
    assert result["truncated"] is True  # the line cut alone truncates
    assert result["match_count"] == 4  # counting continues past the cut
    assert result["file_count"] == 4


@pytest.mark.asyncio
async def test_search_skips_the_reserved_trees(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/.state/obj", b"hit\n")
    await sb.write_file("/workspace/visible.txt", b"hit\n")
    result = await sb.search("/workspace", "hit", skip=(".state",))
    assert result["lines"] == ["visible.txt"]
    # A search that starts INSIDE a reserved tree searches it; paths stay
    # workspace-relative (as the runtime's answer shapes them).
    result_inside = await sb.search("/workspace/.state", "hit")
    assert result_inside["lines"] == [".state/obj"]


@pytest.mark.asyncio
async def test_search_case_insensitive_flag(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/a.txt", b"HIt there\n")
    assert (await sb.search("/workspace", "hit"))["file_count"] == 0
    result = await sb.search("/workspace", "hit", case_insensitive=True)
    assert result["file_count"] == 1


@pytest.mark.asyncio
async def test_search_does_not_follow_symlinked_directories(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/real.txt", b"hit\n")
    os.symlink(str(tmp_path / "real.txt"), str(tmp_path / "link.txt"))
    result = await sb.search("/workspace", "hit")
    assert result["lines"] == ["real.txt"]  # the symlinked file is not read twice
