"""Sandbox tool tests for the protocol 1.8 behaviour (review 2026-11-23).

5.9: the ``edit`` tool is a compare-and-swap (two concurrent edits both
land, or one fails with a clear error instead of clobbering the other).
5.11: ``glob`` and ``grep`` run in the sandbox in ONE round-trip (a walk /
a search), with the legacy per-directory / per-file walk as fallback for a
sandbox (or an older runtime) that does not support the ops.
5.12: ``read`` serves any window of any file with bounded memory (paged
``read_file_range`` reads), including CRLF/CR breaks split across chunk
boundaries and lines longer than the result cap.
5.14: absolute paths are re-checked for containment AFTER normalisation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.model.except_ import BadRequestError
from primer.workspace.local.tools import EditArgs, GlobArgs, GrepArgs, ReadArgs
from primer.workspace.runtime.protocol import ErrorCode
from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeOpError
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.tools import (
    SandboxEdit,
    SandboxGlob,
    SandboxGrep,
    SandboxRead,
)
from primer.workspace.sandbox.tools._common import resolve_sandbox_path
from primer.workspace.tool import ToolCallContext

import primer.workspace.sandbox.tools.read as read_mod


class _StubSession:
    def __init__(self) -> None:
        self._read: set[str] = set()

    def mark_read(self, path: str) -> None:
        self._read.add(path)

    def was_read(self, path: str) -> bool:
        return path in self._read


def _ctx() -> ToolCallContext:
    return ToolCallContext.model_construct(
        workspace_id="ws-1", session_id="sess-a", agent_id="agent-x",
        call_id="call-1", abort=asyncio.Event(),
        session=_StubSession(),  # type: ignore[arg-type]
    )


class _CountingSandbox(FakeSandbox):
    """FakeSandbox that counts the round-trips the tools make."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.list_dir_calls = 0
        self.reads: list[str] = []
        self.walk_calls = 0
        self.search_calls = 0

    async def list_dir(self, path: str):  # type: ignore[override]
        self.list_dir_calls += 1
        return await super().list_dir(path)

    async def read_file(self, path: str) -> bytes:
        self.reads.append(path)
        return await super().read_file(path)

    async def walk(self, path: str, **kwargs):  # type: ignore[override]
        self.walk_calls += 1
        return await super().walk(path, **kwargs)

    async def search(self, path: str, pattern: str, **kwargs):  # type: ignore[override]
        self.search_calls += 1
        raise RuntimeOpError(ErrorCode.EUNSUPPORTED, "Op not implemented: search")


# ---------------------------------------------------------------------------
# 5.9: edit is a compare-and-swap
# ---------------------------------------------------------------------------


class _SlowSandbox(FakeSandbox):
    """Delays reads and CAS writes so concurrent edits interleave."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.cas_calls = 0

    async def read_file(self, path: str) -> bytes:
        await asyncio.sleep(0.05)
        return await super().read_file(path)

    async def write_file_cas(self, path, expected, content, *, mode=None) -> bool:  # type: ignore[override]
        self.cas_calls += 1
        await asyncio.sleep(0.05)
        return await super().write_file_cas(path, expected, content, mode=mode)


async def test_concurrent_non_conflicting_edits_both_land(tmp_path: Path) -> None:
    sb = _SlowSandbox(tmp_path)
    await sb.write_file("/workspace/x.txt", b"alpha one\nbeta two\n")
    tool = SandboxEdit(sb, workspace_root="/workspace")

    results = await asyncio.gather(
        tool.execute(EditArgs(path="x.txt", old_string="one", new_string="ONE"), _ctx()),
        tool.execute(EditArgs(path="x.txt", old_string="two", new_string="TWO"), _ctx()),
        return_exceptions=True,
    )
    # Both land: the second edit's CAS sees the first's write, re-reads and
    # re-applies against the fresh bytes (no lost update).
    assert all(not isinstance(r, Exception) for r in results), results
    body = await sb.read_file("/workspace/x.txt")
    assert body == b"alpha ONE\nbeta TWO\n"
    assert sb.cas_calls >= 2


async def test_concurrent_conflicting_edits_fail_clearly(tmp_path: Path) -> None:
    sb = _SlowSandbox(tmp_path)
    await sb.write_file("/workspace/x.txt", b"foo")
    tool = SandboxEdit(sb, workspace_root="/workspace")

    results = await asyncio.gather(
        tool.execute(EditArgs(path="x.txt", old_string="foo", new_string="bar"), _ctx()),
        tool.execute(EditArgs(path="x.txt", old_string="foo", new_string="baz"), _ctx()),
        return_exceptions=True,
    )
    # Exactly one lands; the other sees the changed file on its retry and
    # fails with a clear error instead of silently clobbering it.
    errors = [r for r in results if isinstance(r, BadRequestError)]
    ok = [r for r in results if not isinstance(r, Exception)]
    assert len(errors) == 1 and len(ok) == 1, results
    assert "file changed during edit" in str(errors[0])
    body = await sb.read_file("/workspace/x.txt")
    assert body in (b"bar", b"baz")


async def test_edit_cas_retries_when_the_file_is_unchanged(tmp_path: Path) -> None:
    """A spurious CAS mismatch (the file bytes did NOT change) is retried
    once and lands, without re-reading for a second mismatch."""

    class _FlakyCasSandbox(_SlowSandbox):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.flaky = 1

        async def write_file_cas(self, path, expected, content, *, mode=None) -> bool:  # type: ignore[override]
            if self.flaky:
                self.flaky = 0
                self.cas_calls += 1
                return False  # a spurious mismatch
            return await super().write_file_cas(path, expected, content, mode=mode)

    sb = _FlakyCasSandbox(tmp_path)
    await sb.write_file("/workspace/x.txt", b"a X b")
    tool = SandboxEdit(sb, workspace_root="/workspace")
    res = await tool.execute(
        EditArgs(path="x.txt", old_string="X", new_string="Y"), _ctx()
    )
    assert b"a Y b" == await sb.read_file("/workspace/x.txt")
    assert "Y" in res.output


async def test_edit_without_cas_uses_the_legacy_write(tmp_path: Path) -> None:
    class _NoCasSandbox(FakeSandbox):
        walk = None  # type: ignore[assignment]
        search = None  # type: ignore[assignment]
        write_file_cas = None  # type: ignore[assignment]

    sb = _NoCasSandbox(tmp_path)
    await sb.write_file("/workspace/x.txt", b"hello world\n")
    tool = SandboxEdit(sb, workspace_root="/workspace")
    await tool.execute(
        EditArgs(path="x.txt", old_string="world", new_string="there"), _ctx()
    )
    assert await sb.read_file("/workspace/x.txt") == b"hello there\n"


async def test_edit_cas_unsupported_falls_back_to_the_legacy_write(
    tmp_path: Path,
) -> None:
    """A WSSandbox over a runtime below protocol 1.8 has the method, but the
    op is answered EUNSUPPORTED: the edit falls back to the legacy
    read-modify-write instead of failing (review 2026-11-23, 5.9)."""

    class _OldRuntimeSandbox(FakeSandbox):
        async def write_file_cas(self, path, expected, content, *, mode=None):  # type: ignore[override]
            raise RuntimeOpError(ErrorCode.EUNSUPPORTED, "Op not implemented: write_file_cas")

    sb = _OldRuntimeSandbox(tmp_path)
    await sb.write_file("/workspace/x.txt", b"hello world\n")
    tool = SandboxEdit(sb, workspace_root="/workspace")
    res = await tool.execute(
        EditArgs(path="x.txt", old_string="world", new_string="there"), _ctx()
    )
    assert await sb.read_file("/workspace/x.txt") == b"hello there\n"
    assert "there" in res.output


# ---------------------------------------------------------------------------
# 5.11: glob and grep in one round-trip
# ---------------------------------------------------------------------------


async def test_glob_uses_one_walk_round_trip(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x")
    (tmp_path / "src" / "util").mkdir()
    (tmp_path / "src" / "util" / "helper.py").write_text("x")
    (tmp_path / "top.py").write_text("x")

    sb = _CountingSandbox(tmp_path)
    tool = SandboxGlob(sb, workspace_root="/workspace")
    res = await tool.execute(GlobArgs(pattern="**/*.py", path="."), _ctx())
    # ONE walk, zero list_dir round-trips.
    assert sb.walk_calls == 1
    assert sb.list_dir_calls == 0
    for name in ("top.py", "src/main.py", "src/util/helper.py"):
        assert name in res.output
    assert res.truncated is False


async def test_glob_walk_truncation_marks_the_result(tmp_path: Path) -> None:
    class _TruncatingSandbox(FakeSandbox):
        async def walk(self, path: str, **kwargs):  # type: ignore[override]
            return [], True

    sb = _TruncatingSandbox(tmp_path)
    (tmp_path / "a.py").write_text("x")
    tool = SandboxGlob(sb, workspace_root="/workspace")
    res = await tool.execute(GlobArgs(pattern="*.py", path="."), _ctx())
    assert res.truncated is True


async def test_glob_falls_back_to_the_legacy_walk_when_walk_is_unsupported(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.txt").write_text("x")

    sb = _CountingSandbox(tmp_path)

    async def _unsupported(path: str, **kwargs):
        raise RuntimeOpError(ErrorCode.EUNSUPPORTED, "Op not implemented: walk")

    sb.walk = _unsupported  # type: ignore[assignment]
    tool = SandboxGlob(sb, workspace_root="/workspace")
    res = await tool.execute(GlobArgs(pattern="*.py", path="."), _ctx())
    # The legacy per-directory walk ran instead.
    assert sb.list_dir_calls >= 1
    assert "a.py" in res.output
    assert "b.txt" not in res.output


async def test_glob_falls_back_for_a_sandbox_without_walk(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")

    sb = _CountingSandbox(tmp_path)
    sb.walk = None  # type: ignore[assignment]
    tool = SandboxGlob(sb, workspace_root="/workspace")
    res = await tool.execute(GlobArgs(pattern="*.py", path="."), _ctx())
    assert sb.list_dir_calls >= 1
    assert "a.py" in res.output


async def test_grep_uses_one_search_round_trip(tmp_path: Path) -> None:
    class _SearchingSandbox(FakeSandbox):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.search_calls = 0
            self.reads: list[str] = []
            self.list_dir_calls = 0

        async def read_file(self, path: str) -> bytes:
            self.reads.append(path)
            return await super().read_file(path)

        async def list_dir(self, path: str):  # type: ignore[override]
            self.list_dir_calls += 1
            return await super().list_dir(path)

        async def search(self, path: str, pattern: str, **kwargs):  # type: ignore[override]
            self.search_calls += 1
            return {
                "lines": ["src/a.py"],
                "match_count": 1,
                "file_count": 1,
                "truncated": False,
                "stop_reason": None,
            }

    sb = _SearchingSandbox(tmp_path)
    tool = SandboxGrep(sb, workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern="TODO"), _ctx())
    # ONE search, zero walk/read round-trips.
    assert sb.search_calls == 1
    assert sb.list_dir_calls == 0
    assert sb.reads == []
    assert res.output == "src/a.py"
    assert res.metadata == {
        "match_count": 1, "file_count": 1, "truncated": False,
    }


async def test_grep_search_caps_mark_the_result_truncated(tmp_path: Path) -> None:
    class _CappedSandbox(FakeSandbox):
        async def search(self, path: str, pattern: str, **kwargs):  # type: ignore[override]
            return {
                "lines": ["a.py"],
                "match_count": 1,
                "file_count": 1,
                "truncated": True,
                "stop_reason": "file_cap",
            }

    sb = _CappedSandbox(tmp_path)
    tool = SandboxGrep(sb, workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern="TODO"), _ctx())
    assert res.truncated is True
    assert res.metadata["truncated"] is True


async def test_grep_regex_only_pattern_goes_to_the_runtime_search(tmp_path: Path) -> None:
    """The runtime matches with the ``regex`` library (the local backend's
    engine), so a regex-only pattern such as the branch reset ``(?|...)`` is
    sent to the one-call search instead of the legacy per-file walk."""
    seen: dict = {}

    class _SearchSandbox(FakeSandbox):
        async def search(self, path: str, pattern: str, **kwargs):  # type: ignore[override]
            seen.update(path=path, pattern=pattern, **kwargs)
            return {"lines": ["src/a.py"], "match_count": 1, "file_count": 1,
                    "truncated": False, "stop_reason": None}

    (tmp_path / "src").mkdir()
    sb = _SearchSandbox(tmp_path)
    tool = SandboxGrep(sb, workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern=r"(?|TODO)", path="src", head_limit=9), _ctx())
    assert res.output == "src/a.py"
    assert seen["pattern"] == r"(?|TODO)"
    assert seen["path"] == "/workspace/src"
    assert seen["head_limit"] == 9  # applied in the runtime, before the transfer
    assert seen["max_output_bytes"] > 0


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"stop_reasons": ["file_cap"]}, "only the first 2000 files (breadth-first"),
        ({"stop_reasons": ["size_cap"], "skipped_large": 3},
         "3 file(s) larger than 1048576 bytes were skipped and not searched"),
        ({"stop_reasons": ["match_cap"]}, "stopped after 5000 matches"),
        ({"stop_reasons": ["time_cap"]}, "30 s deadline"),
        ({"stop_reasons": ["regex_timeout"]}, "the pattern backtracks"),
        ({"stop_reasons": ["output_cap"]}, "output cut at"),
        ({"stop_reason": "file_cap"}, "only the first 2000 files (breadth-first"),
    ],
)
async def test_grep_result_text_names_every_cap_the_search_hit(
    tmp_path: Path, result: dict, expected: str
) -> None:
    class _CappedSandbox(FakeSandbox):
        async def search(self, path: str, pattern: str, **kwargs):  # type: ignore[override]
            return {"lines": ["a.py"], "match_count": 1, "file_count": 1, "truncated": True, **result}

    tool = SandboxGrep(_CappedSandbox(tmp_path), workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern="TODO"), _ctx())
    lines = res.output.split("\n")
    assert lines[0] == "a.py"
    assert any(l.startswith("[note: ") and expected in l for l in lines[1:]), res.output
    assert res.truncated is True


async def test_grep_file_cap_note_names_the_glob_and_does_not_blame_it(tmp_path: Path) -> None:
    class _CappedSandbox(FakeSandbox):
        async def search(self, path: str, pattern: str, **kwargs):  # type: ignore[override]
            return {"lines": [], "match_count": 0, "file_count": 0, "truncated": True,
                    "stop_reason": "file_cap", "stop_reasons": ["file_cap"]}

    tool = SandboxGrep(_CappedSandbox(tmp_path), workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern="x", glob="*.py"), _ctx())
    assert "the first 2000 files matching '*.py'" in res.output
    assert "narrow the path or glob" not in res.output
    assert "search a narrower path" in res.output


async def test_grep_result_has_no_note_when_nothing_was_capped(tmp_path: Path) -> None:
    class _OkSandbox(FakeSandbox):
        async def search(self, path: str, pattern: str, **kwargs):  # type: ignore[override]
            return {"lines": ["a.py"], "match_count": 1, "file_count": 1, "truncated": False,
                    "stop_reason": None, "stop_reasons": [], "skipped_large": 0}

    tool = SandboxGrep(_OkSandbox(tmp_path), workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern="TODO"), _ctx())
    assert res.output == "a.py"


async def test_grep_single_file_target_uses_the_legacy_path(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("TODO\n")
    sb = _CountingSandbox(tmp_path)
    tool = SandboxGrep(sb, workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern="TODO", path="a.py"), _ctx())
    assert sb.search_calls == 0
    assert res.output == "a.py"


async def test_grep_falls_back_to_the_legacy_walk_when_search_is_unsupported(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("TODO a\n")

    sb = _CountingSandbox(tmp_path)  # its search raises EUNSUPPORTED
    tool = SandboxGrep(sb, workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern="TODO"), _ctx())
    assert sb.search_calls == 1
    assert sb.list_dir_calls >= 1
    assert res.output == "src/a.py"


# ---------------------------------------------------------------------------
# Fix round 1: FakeSandbox.walk matches the runtime's order and caps
# ---------------------------------------------------------------------------


async def test_fake_walk_enforces_max_entries_across_nested_directories(tmp_path: Path) -> None:
    for i in range(30):
        (tmp_path / f"d{i:03d}" / "deep").mkdir(parents=True)
        (tmp_path / f"d{i:03d}" / "f.txt").write_text("x")
        (tmp_path / f"d{i:03d}" / "deep" / "g.txt").write_text("x")
    entries, truncated = await FakeSandbox(tmp_path).walk("/workspace", max_entries=10)
    assert truncated is True
    assert len(entries) == 10


async def test_fake_walk_is_breadth_first_in_sorted_order(tmp_path: Path) -> None:
    (tmp_path / "a" / "deep").mkdir(parents=True)
    (tmp_path / "a" / "deep" / "x.txt").write_text("x")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "y.txt").write_text("y")
    (tmp_path / "z.txt").write_text("z")
    entries, truncated = await FakeSandbox(tmp_path).walk("/workspace")
    assert truncated is False
    # The same list the runtime's walk op returns for this tree.
    assert [e["path"] for e in entries] == ["a", "b", "z.txt", "a/deep", "b/y.txt", "a/deep/x.txt"]


async def test_fake_walk_skips_a_directory_that_vanishes_mid_walk(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "y.txt").write_text("y")
    real_iterdir = Path.iterdir

    def flaky(self: Path):
        if self.name == "a":
            raise FileNotFoundError(str(self))  # deleted between the listing and the descent
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", flaky)
    entries, truncated = await FakeSandbox(tmp_path).walk("/workspace")
    assert truncated is False
    assert [e["path"] for e in entries] == ["a", "b", "b/y.txt"]


async def test_fake_walk_honours_the_deadline(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x")
    entries, truncated = await FakeSandbox(tmp_path).walk("/workspace", timeout_s=-1.0)
    assert truncated is True
    assert entries == []


# ---------------------------------------------------------------------------
# 5.12: bounded windowed reads
# ---------------------------------------------------------------------------


async def test_read_window_near_the_end_of_a_large_file(tmp_path: Path) -> None:
    """Any window of a big file is served with bounded memory: the read is
    paged in _CHUNK_BYTES windows (here a 250 KiB file needs several)."""
    sb = FakeSandbox(root=tmp_path)
    lines = [f"line {i:05d} payload" for i in range(10_000)]  # ~250 KiB
    await sb.write_file("/workspace/big.txt", ("\n".join(lines) + "\n").encode())

    tool = SandboxRead(sb, workspace_root="/workspace")
    res = await tool.execute(ReadArgs(path="big.txt", offset=9_990, limit=5), _ctx())
    for i in range(9_990, 9_995):
        assert f"line {i:05d}" in res.output
    assert "line 9995" not in res.output  # the window is [9990, 9995)
    assert res.truncated is True  # the file continues before the window


async def test_read_crlf_split_across_chunk_boundaries(tmp_path: Path, monkeypatch) -> None:
    """A \\r\\n pair split across two chunk reads is ONE break (no phantom
    empty line, no merged line)."""
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"aa\r\nbb\r\ncc\r\n")

    monkeypatch.setattr(read_mod, "_CHUNK_BYTES", 5)
    tool = SandboxRead(sb, workspace_root="/workspace")
    res = await tool.execute(ReadArgs(path="x.txt"), _ctx())
    assert res.output.count("\u2192") == 3
    assert "aa" in res.output and "bb" in res.output and "cc" in res.output
    assert res.truncated is False


async def test_read_lone_cr_breaks_and_trailing_cr(tmp_path: Path, monkeypatch) -> None:
    """A lone \\r is a line break (text-mode semantics); a trailing \\r at
    EOF ends the last line (no phantom empty line)."""
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"x\ry\nz\r")

    monkeypatch.setattr(read_mod, "_CHUNK_BYTES", 2)
    tool = SandboxRead(sb, workspace_root="/workspace")
    res = await tool.execute(ReadArgs(path="x.txt"), _ctx())
    assert res.output.count("\u2192") == 3
    for line in ("x", "y", "z"):
        assert line in res.output
    assert res.truncated is False


async def test_read_line_longer_than_the_cap_is_cut_at_its_head(
    tmp_path: Path,
) -> None:
    from primer.workspace.local.tools._common import TOOL_RESULT_CAP_BYTES

    sb = FakeSandbox(root=tmp_path)
    body = b"head\n" + b"Z" * 60_000 + b"\ntail\n"
    await sb.write_file("/workspace/big.txt", body)

    tool = SandboxRead(sb, workspace_root="/workspace")
    res = await tool.execute(ReadArgs(path="big.txt", offset=1, limit=1), _ctx())
    assert res.truncated is True
    assert "last line cut" in res.output
    assert res.output.count("Z") < 60_000
    assert len(res.output.encode("utf-8")) < TOOL_RESULT_CAP_BYTES + 256


async def test_read_oversized_line_split_across_chunks_is_cut(
    tmp_path: Path, monkeypatch,
) -> None:
    """A line longer than the pending bound, split across chunk reads: its
    head is served cut at the result cap (bounded memory: the rest of the
    line is never retained)."""
    from primer.workspace.local.tools._common import TOOL_RESULT_CAP_BYTES

    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/big.txt", b"Z" * 60_000)

    monkeypatch.setattr(read_mod, "_CHUNK_BYTES", 4096)
    tool = SandboxRead(sb, workspace_root="/workspace")
    res = await tool.execute(ReadArgs(path="big.txt", offset=0, limit=1), _ctx())
    assert res.truncated is True
    assert "last line cut" in res.output
    assert len(res.output.encode("utf-8")) < TOOL_RESULT_CAP_BYTES + 256


async def test_read_legacy_whole_file_when_the_sandbox_has_no_range_read(
    tmp_path: Path,
) -> None:
    class _NoRangeSandbox(FakeSandbox):
        read_file_range = None  # type: ignore[assignment]

    sb = _NoRangeSandbox(tmp_path)
    await sb.write_file("/workspace/x.txt", b"alpha\nbeta\ngamma\n")
    tool = SandboxRead(sb, workspace_root="/workspace")
    res = await tool.execute(ReadArgs(path="x.txt", offset=1, limit=1), _ctx())
    assert "beta" in res.output
    assert "alpha" not in res.output
    assert res.truncated is True


async def test_read_legacy_whole_file_result_is_bounded(tmp_path: Path) -> None:
    """The read tool is self_bounded: its legacy whole-file path (no range read) cuts at the cap too."""
    from primer.workspace.local.tools._common import TOOL_RESULT_CAP_BYTES

    class _NoRangeSandbox(FakeSandbox):
        read_file_range = None  # type: ignore[assignment]

    sb = _NoRangeSandbox(tmp_path)
    await sb.write_file("/workspace/big.txt", (("x" * 100 + "\n") * 1500).encode())
    res = await SandboxRead(sb, workspace_root="/workspace").execute(ReadArgs(path="big.txt"), _ctx())
    assert res.truncated is True
    assert "window cut at" in res.output
    assert len(res.output.encode("utf-8")) < TOOL_RESULT_CAP_BYTES + 256


def test_read_and_exec_declare_themselves_self_bounded() -> None:
    from primer.workspace.sandbox.tools import SandboxExec

    assert SandboxRead.self_bounded is True
    assert SandboxExec.self_bounded is True
    assert SandboxGrep.self_bounded is False and SandboxGlob.self_bounded is False


# ---------------------------------------------------------------------------
# 5.14: containment re-checked after normalisation
# ---------------------------------------------------------------------------


def test_absolute_path_outside_the_root_is_refused() -> None:
    """/workspace/../etc/passwd normalises OUT of the root: refused with a
    clean 400, not returned for the runtime to answer with an EACCES."""
    with pytest.raises(BadRequestError):
        resolve_sandbox_path("/workspace", "/workspace/../etc/passwd")


def test_absolute_path_outside_the_root_deep_escape_is_refused() -> None:
    with pytest.raises(BadRequestError):
        resolve_sandbox_path("/workspace", "/workspace/a/../../etc/passwd")


def test_absolute_path_inside_the_root_is_honoured() -> None:
    assert resolve_sandbox_path("/workspace", "/workspace/sub") == "/workspace/sub"


def test_absolute_path_is_normalised_before_the_containment_check() -> None:
    assert resolve_sandbox_path("/workspace", "/workspace//a/./b") == "/workspace/a/b"
    assert (
        resolve_sandbox_path("/workspace", "/workspace/a/../b") == "/workspace/b"
    )


def test_root_workspace_root_allows_any_path() -> None:
    assert resolve_sandbox_path("/", "/etc/passwd") == "/etc/passwd"


def test_relative_parent_escape_is_still_refused() -> None:
    with pytest.raises(BadRequestError):
        resolve_sandbox_path("/workspace", "../etc/passwd")


# ---------------------------------------------------------------------------
# grep over the REAL FakeSandbox.search: the 1.8 tool path end to end
# (no stubbed search op: the fake's host twin of the runtime op is driven)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_over_the_real_fake_sandbox_search(tmp_path: Path) -> None:
    """The 1.8 grep path end to end: the tool sends ONE search to the
    sandbox (no walk, no per-file reads) and the FakeSandbox's own search
    op answers it (the runtime op's host twin)."""

    class _CountingSandbox(FakeSandbox):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.list_dir_calls = 0
            self.reads: list[str] = []

        async def list_dir(self, path: str):  # type: ignore[override]
            self.list_dir_calls += 1
            return await super().list_dir(path)

        async def read_file(self, path: str) -> bytes:
            self.reads.append(path)
            return await super().read_file(path)

    sb = _CountingSandbox(tmp_path)
    await sb.write_file("/workspace/src/a.py", b"x = 1  # TODO: fix\n")
    await sb.write_file("/workspace/src/b.py", b"# TODO: later\n")
    await sb.write_file("/workspace/doc.md", b"nothing to see here\n")
    tool = SandboxGrep(sb, workspace_root="/workspace")

    res = await tool.execute(GrepArgs(pattern="TODO"), _ctx())
    assert sb.list_dir_calls == 0  # no legacy walk
    # The op reads each candidate through the sandbox read seam (the fake's
    # twin of the runtime op's in-process file reads): exactly the walked
    # candidates, in walk order, no more.
    assert sb.reads == ["/workspace/doc.md", "/workspace/src/a.py", "/workspace/src/b.py"]
    assert res.output == "src/a.py\nsrc/b.py"
    assert res.metadata == {"match_count": 2, "file_count": 2, "truncated": False}

    # count mode over the same real op.
    res_count = await tool.execute(
        GrepArgs(pattern="TODO", output_mode="count"), _ctx()
    )
    assert res_count.output == "src/a.py:1\nsrc/b.py:1"


@pytest.mark.asyncio
async def test_grep_over_the_real_fake_sandbox_search_marks_the_size_cap(
    tmp_path: Path
) -> None:
    """A file over the 1 MiB search cap is skipped and said so: the note can
    only come from the 1.8 search path (the legacy walk has no size cap and
    emits no notes), so its presence proves the real op ran."""
    sb = FakeSandbox(tmp_path)
    await sb.write_file("/workspace/big.txt", b"hit " * 400_000)  # 1.6 MiB
    await sb.write_file("/workspace/small.txt", b"hit\n")
    tool = SandboxGrep(sb, workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern="hit"), _ctx())
    assert "big.txt" not in res.output
    assert res.output.startswith("small.txt")
    assert any(
        l.startswith("[note: ") and "larger than 1048576 bytes" in l
        for l in res.output.split("\n")
    ), res.output
    assert res.truncated is True
