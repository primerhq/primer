"""_WorkspaceIOShim.read_state_file feeds the turn-log writer's seq bootstrap
(01a08bfb item 4). It may return ``b""`` only when there is genuinely nothing
to read; every other failure must raise so the writer does not mistake an
unread log for a brand-new one and restart numbering over existing lines."""

from __future__ import annotations

import json

import pytest

from primer.model.except_ import NotFoundError, PrimerError
from primer.observability.turn_log_writer import WorkspaceTurnLogWriter
from primer.model.turn_log import TurnLogStarted
from primer.worker.io_shim import _WorkspaceIOShim

from datetime import datetime, timezone


class _Workspace:
    state_path = ".state"

    def __init__(self, files: dict[str, bytes] | None = None, *, read_exc=None):
        self.files = files or {}
        self.read_exc = read_exc
        self.appended: list[tuple[str, bytes]] = []

    async def read_file(self, path: str) -> bytes:
        if self.read_exc is not None:
            raise self.read_exc
        if path not in self.files:
            raise NotFoundError(f"{path!r} not found")
        return self.files[path]

    async def append_state_line(self, path: str, line: bytes) -> None:
        self.appended.append((path, line))


class _Registry:
    def __init__(self, workspace):
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str):
        return self._workspace


REL = "sessions/s1/turns.jsonl"


@pytest.mark.asyncio
async def test_returns_file_bytes() -> None:
    ws = _Workspace({".state/" + REL: b'{"seq":1}\n'})
    shim = _WorkspaceIOShim(_Registry(ws))
    assert await shim.read_state_file("w1", REL) == b'{"seq":1}\n'


@pytest.mark.asyncio
async def test_absent_file_is_empty() -> None:
    shim = _WorkspaceIOShim(_Registry(_Workspace()))
    assert await shim.read_state_file("w1", REL) == b""


@pytest.mark.asyncio
async def test_no_registry_is_empty() -> None:
    # append_state_line drops its bytes too when there is no registry.
    assert await _WorkspaceIOShim(None).read_state_file("w1", REL) == b""


@pytest.mark.asyncio
async def test_unresolvable_workspace_raises_not_empty() -> None:
    shim = _WorkspaceIOShim(_Registry(None))
    with pytest.raises(PrimerError) as ei:
        await shim.read_state_file("w1", REL)
    # Must not look like an absent file to the writer.
    assert not isinstance(ei.value, NotFoundError)


@pytest.mark.asyncio
async def test_backend_read_error_raises_not_empty() -> None:
    shim = _WorkspaceIOShim(
        _Registry(_Workspace(read_exc=ConnectionError("ws dropped")))
    )
    with pytest.raises(ConnectionError):
        await shim.read_state_file("w1", REL)


@pytest.mark.asyncio
async def test_transient_read_failure_does_not_write_seq_1_over_existing_log() -> None:
    """End to end through the real writer: the log already holds seq 1-5, the
    first read fails. The old shim returned b"" -> seq bootstrapped at 0 ->
    the next append wrote seq=1 after seq=5."""
    existing = b"".join(
        json.dumps({"seq": n, "kind": "started"}).encode() + b"\n"
        for n in range(1, 6)
    )
    ws = _Workspace({".state/" + REL: existing})
    shim = _WorkspaceIOShim(_Registry(ws))
    flaky = {"fail": True}

    async def _read() -> bytes:
        if flaky["fail"]:
            flaky["fail"] = False
            raise ConnectionError("ws dropped")
        return await shim.read_state_file("w1", REL)

    async def _append(line: bytes) -> None:
        await shim.append_state_line("w1", REL, line)

    writer = WorkspaceTurnLogWriter(append_line=_append, read_existing=_read)
    event = TurnLogStarted(
        seq=0, ts=datetime(2026, 6, 5, tzinfo=timezone.utc),
        model="m", input_message_count=1,
    )

    with pytest.raises(ConnectionError):
        await writer.append(event)
    assert ws.appended == []

    assert await writer.append(event) == 6
    assert json.loads(ws.appended[0][1].decode())["seq"] == 6
