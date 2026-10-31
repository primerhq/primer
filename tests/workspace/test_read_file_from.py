"""``read_file_from(path, offset)``: the bytes of a file from a byte offset, on every workspace and sandbox that offers it (perf plan Task 13)."""
from __future__ import annotations

import pytest

from primer.int.sandbox import Sandbox
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.workspace import WorkspaceTemplate
from primer.workspace.local.workspace import LocalWorkspace


def _local(root) -> LocalWorkspace:
    template = WorkspaceTemplate.model_validate({"id": "tpl", "description": "", "provider_id": "p", "backend": {"kind": "local"}})
    return LocalWorkspace(workspace_id="ws-1", root=root, template=template, env={}, state_repo=None, truncation_store=None, tools=[])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_local_workspace_returns_the_tail_from_an_offset(tmp_path):
    (tmp_path / "log.jsonl").write_bytes(b"0123456789")
    ws = _local(tmp_path)
    for offset, want in ((0, b"0123456789"), (4, b"456789"), (10, b""), (99, b"")):
        assert await ws.read_file_from("log.jsonl", offset) == want, offset
        assert await ws.read_file_from("log.jsonl", offset) == (await ws.read_file("log.jsonl"))[offset:]


@pytest.mark.asyncio
async def test_the_local_workspace_refuses_a_missing_file_and_a_directory_as_read_file_does(tmp_path):
    (tmp_path / "d").mkdir()
    ws = _local(tmp_path)
    with pytest.raises(NotFoundError):
        await ws.read_file_from("nope", 3)
    with pytest.raises(BadRequestError):
        await ws.read_file_from("d", 3)


@pytest.mark.asyncio
async def test_a_sandbox_without_a_ranged_read_falls_back_to_the_whole_file():
    # abstract members of Sandbox are many; the default method is a plain function, so call it unbound on a bare stand-in
    class _Stand:
        async def read_file(self, path):
            return b"abcdef"

    assert await Sandbox.read_file_from(_Stand(), "p", 2) == b"cdef"
