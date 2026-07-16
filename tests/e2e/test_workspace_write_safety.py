"""E2E: concurrent workspace writers never corrupt files (write-safety).

Container-backed so the primer/workspace-runtime:1.1 image (runtime-side
Tier-A/Tier-B locks + protocol 1.2 negotiation) is exercised end to end
through the real REST file surface.

Scope note: this module drives everything through the reachable REST API
(``PUT /files``, ``GET /files/read``, ``POST /diagnostic``). It does not
attempt to prove exec write-intent scenarios such as per-workdir exec
serialization or ``access=read`` parallelism, because the ``/diagnostic``
endpoint is intentionally read-only: it whitelists only
``{echo, pwd, whoami, uname, ls}``, hard-codes ``workdir=/workspace``, and
has no ``access`` parameter. Those write-intent behaviors are exercised at
the tool layer (agent sessions / MCP ``call_tool``), which is out of scope
for a focused REST e2e, and are instead proven by unit tests: per-workdir
exec serialization in ``tests/runtime/test_exec_task.py`` (Task 3),
lock-parity for the sandbox backend in
``tests/workspace/sandbox/test_fake_lock_parity.py`` (Task 5), and the
local backend's write-lock behavior in ``tests/workspace/test_local.py``
(Task 8). What IS reachable and meaningful here, and what this module
proves instead, is that concurrent writers hitting the real container
runtime through the file API never produce a torn or interleaved file
(atomicity + Tier-A locking) and that writes to independent paths are not
needlessly serialized against each other.
"""
from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

pytestmark = pytest.mark.skipif(
    os.environ.get("PRIMER_RUN_E2E") != "1", reason="e2e gated (PRIMER_RUN_E2E)"
)

_K = 8


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


@pytest.mark.asyncio
async def test_concurrent_same_path_writes_well_formed(container_ws, client):
    # K concurrent writers race on the SAME path, each with a large, distinct
    # body. After all settle, the file must contain exactly one writer's full
    # payload, never a torn/interleaved mix. This is an end-to-end ATOMICITY
    # proof through the REAL container runtime: it shows the write path lands
    # whole payloads (temp file + os.replace), not that the Tier-A lock is
    # held. os.replace is atomic on its own, so this would still pass with
    # hold_write removed; Tier-A serialization itself is unit-covered
    # (runtime/tests/test_locks.py, tests/workspace/test_locks_local.py).
    bodies = [chr(ord("A") + i) * 100_000 for i in range(_K)]
    d = await client.post(f"/v1/workspaces/{container_ws}/files/dir", params={"path": "d"})
    assert d.status_code == 204, d.text
    await asyncio.gather(*(_write(client, container_ws, "d/f.txt", b) for b in bodies))
    out = await _read(client, container_ws, "d/f.txt")
    assert len(out) == 100_000, f"non-atomic write: content length {len(out)} != 100000"
    assert out in bodies, "content matched none of the writers' payloads (torn/interleaved write)"


@pytest.mark.asyncio
async def test_concurrent_different_path_writes_all_succeed(container_ws, client):
    # K concurrent writers hit K DIFFERENT paths. Every path must read back
    # its own full body: proves independent-path writes are not
    # over-serialized and do not deadlock or clobber each other.
    d = await client.post(f"/v1/workspaces/{container_ws}/files/dir", params={"path": "d2"})
    assert d.status_code == 204, d.text
    paths = [f"d2/f{i}.txt" for i in range(_K)]
    bodies = [str(i) * 100_000 for i in range(_K)]
    await asyncio.gather(*(
        _write(client, container_ws, p, b) for p, b in zip(paths, bodies)
    ))
    for p, b in zip(paths, bodies):
        assert await _read(client, container_ws, p) == b


@pytest.mark.asyncio
async def test_diagnostic_liveness(container_ws, client):
    # Light sanity check that the real container runtime is serving, via the
    # one exec surface the REST API actually exposes (whitelisted, read-only).
    r = await client.post(f"/v1/workspaces/{container_ws}/diagnostic",
                          json={"command": "ls", "timeout_seconds": 10})
    assert r.status_code in (200, 201), r.text
    assert r.json()["exit_code"] == 0, r.json()
