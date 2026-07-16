"""FakeSandbox lock parity: same-workdir write execs serialize; reads don't."""

import asyncio
import pytest
from primer.workspace.sandbox.fake import FakeSandbox


@pytest.mark.asyncio
async def test_fake_same_workdir_execs_serialize(tmp_path):
    sb = FakeSandbox(tmp_path)
    # Each exec is a mutual-exclusion probe: if the marker already exists
    # a concurrent exec is mid-flight -> exit 42 (overlap detected).
    # Serialized execution means the second exec only runs AFTER the first
    # removed the marker, so both exit 0.
    probe = (
        "if [ -e busy.marker ]; then exit 42; fi; "
        "touch busy.marker; sleep 0.05; rm -f busy.marker"
    )
    r1, r2 = await asyncio.gather(
        sb.exec(probe, workdir="/workspace"),
        sb.exec(probe, workdir="/workspace"),
    )
    assert r1.exit_code == 0 and r2.exit_code == 0, "same-workdir execs overlapped"


@pytest.mark.asyncio
async def test_fake_read_access_is_parallel(tmp_path):
    sb = FakeSandbox(tmp_path)
    started = asyncio.Event()

    async def slow():
        await sb.exec("sleep 0.15", workdir="/workspace")

    async def quick_read():
        await asyncio.sleep(0.02)
        await asyncio.wait_for(
            sb.exec("true", workdir="/workspace", access="read"), timeout=0.1,
        )
        started.set()

    await asyncio.gather(slow(), quick_read())
    assert started.is_set()
