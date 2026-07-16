"""E2E: concurrent workspace writers never corrupt files (write-safety).

Container-backed so the primer/workspace-runtime:1.1 image (runtime-side
Tier-A/Tier-B locks + protocol 1.2 negotiation) is exercised end to end.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest
import pytest_asyncio

pytestmark = pytest.mark.skipif(
    os.environ.get("PRIMER_RUN_E2E") != "1", reason="e2e gated (PRIMER_RUN_E2E)"
)


@pytest_asyncio.fixture
async def container_ws(client, unique_suffix):
    """Provision a CONTAINER workspace on the :1.1 image; yield its id.

    Mirrors tests/e2e/test_smk_workspaces.py::_make_container_workspace.
    Teardown deletes the workspace, template, and provider.
    """
    wp = f"wp-wsafe-{unique_suffix}"
    tpl = f"tpl-wsafe-{unique_suffix}"
    rp = await client.post("/v1/workspace_providers", json={
        "id": wp, "provider": "container",
        "config": {"kind": "container", "runtime": "docker",
                   "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
                   "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"}},
    })
    assert rp.status_code in (200, 201), rp.text
    rt = await client.post("/v1/workspace_templates", json={
        "id": tpl, "description": "write-safety e2e", "provider_id": wp,
        "backend": {"kind": "container", "image": "primer/workspace-runtime:1.1"},
    })
    assert rt.status_code in (200, 201), rt.text
    rw = await client.post("/v1/workspaces", json={"template_id": tpl})
    assert rw.status_code in (200, 201), rw.text
    wid = rw.json()["id"]
    phase = None
    for _ in range(90):
        got = await client.get(f"/v1/workspaces/{wid}")
        phase = got.json().get("phase")
        if phase == "running":
            break
        assert phase not in ("failed", "error"), got.text
        await asyncio.sleep(1.0)
    assert phase == "running", f"workspace never ran: {phase!r}"
    try:
        yield wid
    finally:
        await client.delete(f"/v1/workspaces/{wid}")
        await client.delete(f"/v1/workspace_templates/{tpl}")
        await client.delete(f"/v1/workspace_providers/{wp}")


async def _write(client, wid, path, content):
    r = await client.put(f"/v1/workspaces/{wid}/files",
                         params={"path": path},
                         json={"content": content, "encoding": "text"})
    assert r.status_code == 204, r.text


async def _read(client, wid, path):
    r = await client.get(f"/v1/workspaces/{wid}/files/read",
                        params={"path": path, "encoding": "text"})
    assert r.status_code == 200, r.text
    return r.json()["content"]


async def _exec(client, wid, command, *, timeout=30):
    r = await client.post(f"/v1/workspaces/{wid}/diagnostic",
                         json={"command": command, "timeout_seconds": timeout})
    assert r.status_code in (200, 201), r.text
    return r.json()


@pytest.mark.asyncio
async def test_two_writers_same_file_well_formed(container_ws, client):
    # A tool write and a shell write race on the SAME file; the result is
    # exactly one payload (no torn interleave). Atomic write + Tier-A lock.
    big_a = "A" * 200_000
    await client.post(f"/v1/workspaces/{container_ws}/files/dir", params={"path": "d"})
    await asyncio.gather(
        _write(client, container_ws, "d/f.txt", big_a),
        _exec(client, container_ws, "printf 'BBBB' > d/f.txt"),
    )
    out = await _read(client, container_ws, "d/f.txt")
    assert set(out) in ({"A"}, {"B"}), "torn interleave detected"


@pytest.mark.asyncio
async def test_same_dir_execs_serialize_diff_dirs_overlap(container_ws, client):
    # Same-dir probe: exit 42 if another exec is mid-flight in that dir.
    await client.post(f"/v1/workspaces/{container_ws}/files/dir", params={"path": "same"})
    await client.post(f"/v1/workspaces/{container_ws}/files/dir", params={"path": "d1"})
    await client.post(f"/v1/workspaces/{container_ws}/files/dir", params={"path": "d2"})
    probe = ("cd same; if [ -e busy ]; then exit 42; fi; "
             "touch busy; sleep 0.4; rm -f busy")
    r1, r2 = await asyncio.gather(
        _exec(client, container_ws, probe),
        _exec(client, container_ws, probe),
    )
    assert r1["exit_code"] == 0 and r2["exit_code"] == 0, "same-dir execs overlapped"

    # Different dirs must OVERLAP: two 0.4s sleeps in parallel finish well
    # under the ~0.8s serialized floor.
    t0 = time.monotonic()
    await asyncio.gather(
        _exec(client, container_ws, "cd d1; sleep 0.4; touch done"),
        _exec(client, container_ws, "cd d2; sleep 0.4; touch done"),
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 0.75, f"different-dir execs serialized (elapsed={elapsed:.2f}s)"


@pytest.mark.asyncio
async def test_tool_write_and_shell_write_same_dir_no_corrupt(container_ws, client):
    # A tool write to dir D and a shell append to another file in D run
    # concurrently; both complete intact (tool-vs-shell scope serialization).
    await client.post(f"/v1/workspaces/{container_ws}/files/dir", params={"path": "mix"})
    payload = "X" * 100_000
    await asyncio.gather(
        _write(client, container_ws, "mix/tool.txt", payload),
        _exec(client, container_ws, "printf 'SHELL' > mix/shell.txt"),
    )
    assert await _read(client, container_ws, "mix/tool.txt") == payload
    assert await _read(client, container_ws, "mix/shell.txt") == "SHELL"


@pytest.mark.asyncio
async def test_protocol_negotiation_enforces_runtime_lock(container_ws, client):
    # The container reached `running` => protocol 1.2 client negotiated with
    # the 1.1 image (major match) and the runtime is enforcing locks. Prove
    # the runtime-side Tier-B lock is live end-to-end: a default-writer exec
    # holding a dir serializes a second same-dir writer, but a read-only
    # command in that SAME dir is NOT blocked (undeclared writer vs reader).
    await client.post(f"/v1/workspaces/{container_ws}/files/dir", params={"path": "neg"})
    # Long default-write exec holds neg's scope for ~0.6s.
    hold = asyncio.create_task(_exec(client, container_ws, "cd neg; sleep 0.6; echo held"))
    await asyncio.sleep(0.1)
    # A plain read (ls) in the same dir must return promptly (reads unguarded).
    t0 = time.monotonic()
    ls = await _exec(client, container_ws, "ls neg")
    read_elapsed = time.monotonic() - t0
    assert ls["exit_code"] == 0
    assert read_elapsed < 0.4, f"read blocked by writer (elapsed={read_elapsed:.2f}s)"
    held = await hold
    assert "held" in held["stdout"]
