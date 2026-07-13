"""Regression: the mount manifest (`.state/mounts.json`) lives under the
reserved ``.state`` tree, which the public ``write_file`` refuses to mutate.

The original ``save_manifest`` used ``write_file`` and therefore raised
``BadRequestError`` ("refusing to mutate path inside reserved tree") on every
mount against the REAL local/sandbox backends — the bug only hid because the
in-memory test fakes never enforced the reserved-tree guard. These tests run
against a real ``LocalWorkspaceBackend`` so the guard is live.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.except_ import BadRequestError
from primer.workspace import LocalWorkspaceBackend
from primer.workspace import mount_manifest as mm
from tests.workspace.test_local import _template


@pytest.fixture
async def ws(tmp_path):
    backend = LocalWorkspaceBackend(tmp_path / "root")
    await backend.initialize()
    return await backend.create(_template())


async def test_write_file_still_refuses_reserved_state(ws) -> None:
    # The public guard must stay intact -- only the privileged path may write.
    with pytest.raises(BadRequestError):
        await ws.write_file(".state/mounts.json", b"x")


async def test_write_state_file_bypasses_guard_and_roundtrips(ws) -> None:
    payload = b'{"version": 1, "mounts": []}'
    await ws.write_state_file(".state/mounts.json", payload)
    assert await ws.read_file(".state/mounts.json") == payload


async def test_write_state_file_rejects_root_escape(ws) -> None:
    with pytest.raises(BadRequestError):
        await ws.write_state_file("../evil.json", b"x")


async def test_save_manifest_roundtrips_through_real_backend(ws) -> None:
    entry = mm.MountEntry(
        mount_id="wsmnt-abc123",
        collection_id="kb-1",
        collection_name="kb-1",
        dest="kb-1",
        mounted_at=datetime.now(timezone.utc),
        base=[],
    )
    await mm.save_manifest(ws, mm.add_mount(mm.MountManifest(), entry))
    loaded = await mm.load_manifest(ws)
    assert [e.mount_id for e in loaded.mounts] == ["wsmnt-abc123"]
    assert loaded.mounts[0].collection_name == "kb-1"
