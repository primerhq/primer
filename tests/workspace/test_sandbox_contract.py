"""Sandbox ABC contract test suite.

Parametrized over every concrete :class:`~matrix.int.sandbox.Sandbox` impl so
that any implementation can be verified against the same set of behavioural
assertions.

Current fixtures
----------------
* ``fake_sandbox``  — :class:`~matrix.workspace.sandbox.fake.FakeSandbox`
  backed by a host ``tmp_path``; no network, no containers.

* ``ws_sandbox``    — :class:`~matrix.workspace.runtime.ws_sandbox.WSSandbox`
  backed by a real :class:`~matrix.workspace.runtime.runtime_client.RuntimeClient`
  that speaks to an in-process ``aiohttp`` test server running the actual
  ``matrix_runtime.server`` code against a ``tmp_path``.  No Docker required.

Adding a new implementation
---------------------------
1. Add a new entry to ``_SANDBOX_FACTORIES`` at the bottom of this module that
   maps a string name to a ``SandboxFactory`` callable.
2. All existing contract tests pick it up automatically via the parametrized
   ``sandbox`` fixture.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from matrix.int.sandbox import FileStat, Sandbox, SandboxInspectInfo
from matrix.workspace.runtime.runtime_client import RuntimeClient
from matrix.workspace.runtime.ws_sandbox import WSSandbox
from matrix.workspace.sandbox.fake import FakeSandbox


# ---------------------------------------------------------------------------
# Sandbox factory protocol
# ---------------------------------------------------------------------------


class SandboxFactory(Protocol):
    """An async callable that builds + tears down a Sandbox for one test."""

    async def __call__(self, tmp_path: Path) -> AsyncIterator[Sandbox]: ...


# ---------------------------------------------------------------------------
# FakeSandbox factory
# ---------------------------------------------------------------------------


async def _make_fake_sandbox(tmp_path: Path) -> AsyncIterator[Sandbox]:
    """Yield a FakeSandbox rooted at *tmp_path/workspace*."""
    sb = FakeSandbox(tmp_path / "workspace", sandbox_id="fake-contract")
    yield sb
    # FakeSandbox doesn't own the tmp_path lifecycle; pytest cleans it up.


# ---------------------------------------------------------------------------
# WSSandbox factory — in-process aiohttp server + real RuntimeClient
# ---------------------------------------------------------------------------


async def _make_ws_sandbox(tmp_path: Path) -> AsyncIterator[Sandbox]:
    """Yield a WSSandbox connected to an in-process runtime server.

    Spins up ``matrix_runtime.server.build_app`` via aiohttp's ``TestServer``
    so no Docker is involved.  A real :class:`RuntimeClient` connects and
    completes the ``hello`` handshake before the fixture is yielded.
    """
    # Import here so the runtime package is loaded lazily (it lives in
    # ``runtime/`` which is on the pytest pythonpath via pyproject.toml).
    from matrix_runtime.server import build_app  # type: ignore[import-untyped]

    token = "contract-test-token"
    workspace_root = str(tmp_path / "workspace")
    Path(workspace_root).mkdir(parents=True, exist_ok=True)

    app = build_app(token=token, workspace_root=workspace_root)
    test_server = TestServer(app)
    await test_server.start_server()

    url = str(test_server.make_url("/")).replace("http://", "ws://")
    client = RuntimeClient(url=url, token=token)
    await client.connect()

    sandbox: Sandbox = WSSandbox(
        runtime_client=client,
        container_id="ws-contract",
        workspace_root=workspace_root,
    )
    try:
        yield sandbox
    finally:
        await client.aclose()
        await test_server.close()


# ---------------------------------------------------------------------------
# Registry of all sandbox implementations to test
# ---------------------------------------------------------------------------

_SANDBOX_FACTORIES: dict[str, SandboxFactory] = {
    "fake_sandbox": _make_fake_sandbox,  # type: ignore[dict-item]
    "ws_sandbox": _make_ws_sandbox,  # type: ignore[dict-item]
}


# ---------------------------------------------------------------------------
# Parametrized fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(params=list(_SANDBOX_FACTORIES.keys()))
async def sandbox(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Sandbox]:
    """Parametrized fixture that yields each Sandbox implementation in turn.

    Each parameter is a key in ``_SANDBOX_FACTORIES``; all contract tests
    receive a fresh, fully-initialised Sandbox for each parametrization.
    """
    factory = _SANDBOX_FACTORIES[request.param]
    async for sb in factory(tmp_path):
        yield sb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_file_stat(obj: object) -> bool:
    return isinstance(obj, FileStat)


# ---------------------------------------------------------------------------
# Contract: write_file / read_file round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_read_roundtrip(sandbox: Sandbox) -> None:
    """write_file followed by read_file returns the exact bytes written."""
    content = b"hello contract world\n"
    await sandbox.write_file("roundtrip.txt", content)
    result = await sandbox.read_file("roundtrip.txt")
    assert result == content


@pytest.mark.asyncio
async def test_write_read_binary(sandbox: Sandbox) -> None:
    """Binary bytes (including NUL) survive the round-trip intact."""
    content = bytes(range(256))
    await sandbox.write_file("binary.bin", content)
    result = await sandbox.read_file("binary.bin")
    assert result == content


@pytest.mark.asyncio
async def test_write_overwrites(sandbox: Sandbox) -> None:
    """A second write_file replaces the first content completely."""
    await sandbox.write_file("overwrite.txt", b"first")
    await sandbox.write_file("overwrite.txt", b"second")
    result = await sandbox.read_file("overwrite.txt")
    assert result == b"second"


@pytest.mark.asyncio
async def test_read_missing_raises(sandbox: Sandbox) -> None:
    """read_file on a non-existent path raises an exception."""
    with pytest.raises(Exception):
        await sandbox.read_file("does_not_exist_xyz.txt")


# ---------------------------------------------------------------------------
# Contract: list_dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dir_returns_written_file(sandbox: Sandbox) -> None:
    """A file written to a directory appears in list_dir results."""
    await sandbox.write_file("subdir/a.txt", b"a")
    await sandbox.write_file("subdir/b.txt", b"b")
    entries = await sandbox.list_dir("subdir")
    names = {e.path.split("/")[-1] for e in entries}
    assert "a.txt" in names
    assert "b.txt" in names


@pytest.mark.asyncio
async def test_list_dir_returns_filestat_objects(sandbox: Sandbox) -> None:
    """list_dir returns a list of FileStat instances."""
    await sandbox.write_file("statdir/x.txt", b"x")
    entries = await sandbox.list_dir("statdir")
    assert len(entries) >= 1
    for e in entries:
        assert _is_file_stat(e), f"Expected FileStat, got {type(e)}"


@pytest.mark.asyncio
async def test_list_dir_file_has_correct_kind(sandbox: Sandbox) -> None:
    """Files appear with kind='file' in list_dir."""
    await sandbox.write_file("kinddir/f.txt", b"content")
    entries = await sandbox.list_dir("kinddir")
    file_entries = [e for e in entries if e.path.endswith("f.txt")]
    assert len(file_entries) == 1
    assert file_entries[0].kind == "file"


@pytest.mark.asyncio
async def test_list_dir_size_matches_content(sandbox: Sandbox) -> None:
    """The size_bytes in list_dir matches the actual file content length."""
    data = b"size-check-content"
    await sandbox.write_file("sizedir/s.txt", data)
    entries = await sandbox.list_dir("sizedir")
    file_entries = [e for e in entries if e.path.endswith("s.txt")]
    assert len(file_entries) == 1
    assert file_entries[0].size_bytes == len(data)


# ---------------------------------------------------------------------------
# Contract: stat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stat_returns_filestat_for_existing_file(sandbox: Sandbox) -> None:
    """stat returns a FileStat for a file that exists."""
    await sandbox.write_file("stat_test.txt", b"stat content")
    result = await sandbox.stat("stat_test.txt")
    assert result is not None
    assert _is_file_stat(result)
    assert result.kind == "file"


@pytest.mark.asyncio
async def test_stat_size_matches_content(sandbox: Sandbox) -> None:
    """stat reports the correct file size."""
    data = b"twelve bytes"
    await sandbox.write_file("stat_size.txt", data)
    result = await sandbox.stat("stat_size.txt")
    assert result is not None
    assert result.size_bytes == len(data)


@pytest.mark.asyncio
async def test_stat_returns_none_for_missing(sandbox: Sandbox) -> None:
    """stat returns None (not raises) for a path that does not exist."""
    result = await sandbox.stat("no_such_file_12345.txt")
    assert result is None


@pytest.mark.asyncio
async def test_stat_has_modified_at(sandbox: Sandbox) -> None:
    """stat returns a FileStat with a non-None modified_at timestamp."""
    await sandbox.write_file("ts_test.txt", b"ts")
    result = await sandbox.stat("ts_test.txt")
    assert result is not None
    assert result.modified_at is not None


# ---------------------------------------------------------------------------
# Contract: delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_file(sandbox: Sandbox) -> None:
    """delete removes a file; subsequent stat returns None."""
    await sandbox.write_file("to_delete.txt", b"bye")
    await sandbox.delete("to_delete.txt")
    result = await sandbox.stat("to_delete.txt")
    assert result is None


@pytest.mark.asyncio
async def test_delete_removed_file_unreadable(sandbox: Sandbox) -> None:
    """delete removes a file; subsequent read_file raises an exception."""
    await sandbox.write_file("gone.txt", b"gone")
    await sandbox.delete("gone.txt")
    with pytest.raises(Exception):
        await sandbox.read_file("gone.txt")


@pytest.mark.asyncio
async def test_delete_clears_list_dir_entry(sandbox: Sandbox) -> None:
    """delete removes a file from list_dir results."""
    await sandbox.write_file("deldir/keep.txt", b"keep")
    await sandbox.write_file("deldir/remove.txt", b"remove")
    await sandbox.delete("deldir/remove.txt")
    entries = await sandbox.list_dir("deldir")
    names = {e.path.split("/")[-1] for e in entries}
    assert "keep.txt" in names
    assert "remove.txt" not in names


# ---------------------------------------------------------------------------
# Contract: append_line
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_line_creates_file(sandbox: Sandbox) -> None:
    """append_line creates the file if it does not exist."""
    offset = await sandbox.append_line("created_by_append.txt", b"first line")
    assert isinstance(offset, int)
    assert offset >= 0
    content = await sandbox.read_file("created_by_append.txt")
    assert b"first line" in content


@pytest.mark.asyncio
async def test_append_line_appends_successive_lines(sandbox: Sandbox) -> None:
    """Successive append_line calls accumulate all lines in the file."""
    await sandbox.append_line("multi.log", b"line one")
    await sandbox.append_line("multi.log", b"line two")
    await sandbox.append_line("multi.log", b"line three")
    content = await sandbox.read_file("multi.log")
    assert b"line one" in content
    assert b"line two" in content
    assert b"line three" in content


@pytest.mark.asyncio
async def test_append_line_returns_int_offset(sandbox: Sandbox) -> None:
    """append_line returns an integer byte offset."""
    offset = await sandbox.append_line("offset_test.log", b"data")
    assert isinstance(offset, int)
    assert offset >= 0


@pytest.mark.asyncio
async def test_append_line_offset_grows(sandbox: Sandbox) -> None:
    """Successive append_line offsets are non-decreasing."""
    o1 = await sandbox.append_line("grow.log", b"first")
    o2 = await sandbox.append_line("grow.log", b"second")
    # Each new offset must be strictly greater (file grew after first write)
    assert o2 > o1


@pytest.mark.asyncio
async def test_append_line_file_grows(sandbox: Sandbox) -> None:
    """File size increases with each append_line call."""
    await sandbox.append_line("size_grow.log", b"abc")
    s1 = await sandbox.stat("size_grow.log")
    assert s1 is not None
    size_after_first = s1.size_bytes

    await sandbox.append_line("size_grow.log", b"defgh")
    s2 = await sandbox.stat("size_grow.log")
    assert s2 is not None
    assert s2.size_bytes > size_after_first


@pytest.mark.asyncio
async def test_append_line_concurrent_writes_no_interleaving(sandbox: Sandbox) -> None:
    """Concurrent append_line calls must not interleave lines.

    Each written line must appear wholly in the final file.  We do not
    assert ordering because ordering is not part of the contract — only
    atomicity (no partial-write interleaving) is guaranteed.

    Note: ``FakeSandbox`` uses the ABC's default read-modify-write and is
    explicitly documented as not race-safe under concurrent writers.  The
    test is therefore marked ``xfail`` for that implementation.
    """
    # FakeSandbox uses the ABC's default read-modify-write and is not race-safe.
    # Detect by checking the fixture parametrization ID in the node name.
    if isinstance(sandbox, FakeSandbox):
        pytest.xfail(
            "FakeSandbox.append_line uses ABC default (read-modify-write) which "
            "is not race-safe under concurrent writers — this is expected."
        )

    n = 20
    line_template = b"concurrent-line-%03d"
    coros = [sandbox.append_line("concurrent.log", line_template % i) for i in range(n)]
    await asyncio.gather(*coros)

    content = await sandbox.read_file("concurrent.log")
    for i in range(n):
        assert (line_template % i) in content, (
            f"Line {i} missing from concurrent append result"
        )


# ---------------------------------------------------------------------------
# Contract: inspect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_returns_sandbox_inspect_info(sandbox: Sandbox) -> None:
    """inspect() returns a SandboxInspectInfo with a valid state string."""
    info = await sandbox.inspect()
    assert isinstance(info, SandboxInspectInfo)
    assert info.state in {"created", "running", "stopped", "exited", "failed", "unknown"}


# ---------------------------------------------------------------------------
# Contract: is-a Sandbox
# ---------------------------------------------------------------------------


def test_sandbox_is_subclass(sandbox: Sandbox) -> None:  # type: ignore[misc]
    """The parametrized fixture yields an actual Sandbox instance."""
    assert isinstance(sandbox, Sandbox)
