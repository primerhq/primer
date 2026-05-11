"""Tests for SandboxTruncationStore against FakeSandbox."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from matrix.workspace.sandbox.cache import SandboxTruncationStore
from matrix.workspace.sandbox.fake import FakeSandbox


@pytest.mark.asyncio
async def test_write_and_path(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    store = SandboxTruncationStore(sb, root="/workspace/.tmp")
    path = await store.write("hello", session_id="sess-a")
    assert path.startswith("/workspace/.tmp/sess-a/tool_")
    body = await sb.read_file(path)
    assert body == b"hello"


@pytest.mark.asyncio
async def test_output_below_limits_passthrough(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    store = SandboxTruncationStore(sb, root="/workspace/.tmp")
    res = await store.output("short", session_id="sess-a")
    assert res.truncated is False
    assert res.content == "short"


@pytest.mark.asyncio
async def test_output_above_byte_limit_truncates(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    store = SandboxTruncationStore(sb, root="/workspace/.tmp", max_bytes=10)
    res = await store.output("x" * 1000, session_id="sess-a")
    assert res.truncated is True
    assert res.output_path is not None
    assert "Full output saved to" in res.content


@pytest.mark.asyncio
async def test_output_above_line_limit_truncates(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    store = SandboxTruncationStore(sb, root="/workspace/.tmp", max_lines=2)
    res = await store.output("a\nb\nc\nd\n", session_id="sess-a")
    assert res.truncated is True


@pytest.mark.asyncio
async def test_cleanup_session(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    store = SandboxTruncationStore(sb, root="/workspace/.tmp")
    await store.write("a", session_id="sess-x")
    await store.write("b", session_id="sess-x")
    removed = await store.cleanup_session("sess-x")
    assert removed >= 2
    # Subdir is gone.
    assert await sb.stat("/workspace/.tmp/sess-x") is None


@pytest.mark.asyncio
async def test_cleanup_retention(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    # Very short retention so files just written are past it after a tick.
    store = SandboxTruncationStore(
        sb, root="/workspace/.tmp",
        retention=timedelta(microseconds=1),
    )
    await store.write("hi", session_id="sess-a")
    import time

    time.sleep(0.05)
    removed = await store.cleanup()
    assert removed >= 1


@pytest.mark.asyncio
async def test_invalid_session_id_rejected(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    store = SandboxTruncationStore(sb, root="/workspace/.tmp")
    with pytest.raises(ValueError):
        await store.write("x", session_id="../bad")
