"""SMK workspace tests (Phase 2): file CRUD, mkdir, recursive delete, reserved
trees, download, git log, rename, pause/resume, diagnostic, and (via scripted
agents) tool dispatch + two-agent collaboration.

WSP-12 (container) and WSP-13 (kubernetes) are gated on those backends.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_graph,
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    start_graph_session,
    wait_terminal,
)
from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = pytest.mark.asyncio


def _k8s_runtime_image(tag: str) -> str:
    """Cluster-pullable workspace-runtime image for the k8s lane.

    ``PRIMER_K8S_RUNTIME_IMAGE`` overrides the whole reference. Otherwise the
    image is built from ``PRIMER_K8S_REGISTRY`` (the in-cluster registry
    host:port, default ``localhost:30500``) plus the requested tag, so no
    machine-specific node address is baked into the repo.
    """
    import os

    explicit = os.environ.get("PRIMER_K8S_RUNTIME_IMAGE")
    if explicit:
        return explicit
    registry = os.environ.get("PRIMER_K8S_REGISTRY", "localhost:30500")
    return f"{registry}/primer/workspace-runtime:{tag}"


async def _ws(client, suffix, tmp_path):
    return await make_local_workspace(client, suffix=suffix, root=tmp_path)


@smk("SMK-WSP-01", "SMK-WSP-02")
async def test_provider_template_workspace_crud(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    got = await authed_client.get(f"/v1/workspaces/{wid}")
    assert got.status_code == 200, got.text


@smk("SMK-WSP-03")
async def test_rename_workspace(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    r = await authed_client.patch(f"/v1/workspaces/{wid}", json={"name": "Renamed WS"})
    assert r.status_code in (200, 204), r.text
    got = await authed_client.get(f"/v1/workspaces/{wid}")
    assert got.json().get("name") == "Renamed WS"


@smk("SMK-WSP-04", "SMK-WSP-05")
async def test_write_read_info_list(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    w = await authed_client.put(
        f"/v1/workspaces/{wid}/files", params={"path": "notes.txt"},
        json={"content": "hello", "encoding": "text"},
    )
    assert w.status_code == 204, w.text
    rd = await authed_client.get(
        f"/v1/workspaces/{wid}/files/read", params={"path": "notes.txt", "encoding": "text"}
    )
    assert rd.json()["content"] == "hello"
    info = await authed_client.get(f"/v1/workspaces/{wid}/files/info", params={"path": "notes.txt"})
    assert info.json()["kind"] == "file"
    listing = await authed_client.get(f"/v1/workspaces/{wid}/files", params={"path": "."})
    assert any(e["path"] == "notes.txt" for e in listing.json()["items"])


@smk("SMK-WSP-06", "SMK-WSP-07")
async def test_mkdir_and_nested_parents(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    d = await authed_client.post(f"/v1/workspaces/{wid}/files/dir", params={"path": "src/utils"})
    assert d.status_code == 204, d.text
    info = await authed_client.get(f"/v1/workspaces/{wid}/files/info", params={"path": "src/utils"})
    assert info.json()["kind"] == "dir"
    # nested write auto-creates parents
    w = await authed_client.put(
        f"/v1/workspaces/{wid}/files", params={"path": "a/b/c.txt"},
        json={"content": "x", "encoding": "text"},
    )
    assert w.status_code == 204, w.text
    again = await authed_client.post(f"/v1/workspaces/{wid}/files/dir", params={"path": "src/utils"})
    assert again.status_code == 400  # already exists


@smk("SMK-WSP-08")
async def test_delete_file_and_recursive_dir(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    await authed_client.put(f"/v1/workspaces/{wid}/files", params={"path": "d/a.txt"}, json={"content": "x", "encoding": "text"})
    df = await authed_client.delete(f"/v1/workspaces/{wid}/files", params={"path": "d/a.txt"})
    assert df.status_code == 204
    await authed_client.put(f"/v1/workspaces/{wid}/files", params={"path": "d/b.txt"}, json={"content": "y", "encoding": "text"})
    refused = await authed_client.delete(f"/v1/workspaces/{wid}/files", params={"path": "d"})
    assert refused.status_code == 400  # non-empty without recursive
    ok = await authed_client.delete(f"/v1/workspaces/{wid}/files", params={"path": "d", "recursive": "true"})
    assert ok.status_code == 204


@smk("SMK-WSP-09")
async def test_reserved_trees_protected(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    w = await authed_client.put(f"/v1/workspaces/{wid}/files", params={"path": ".state/x"}, json={"content": "x", "encoding": "text"})
    assert w.status_code == 400
    d = await authed_client.post(f"/v1/workspaces/{wid}/files/dir", params={"path": ".tmp/y"})
    assert d.status_code == 400


@smk("SMK-WSP-11")
async def test_download_file(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    await authed_client.put(f"/v1/workspaces/{wid}/files", params={"path": "data.bin"}, json={"content": "BYTES", "encoding": "text"})
    dl = await authed_client.get(f"/v1/workspaces/{wid}/files/download", params={"path": "data.bin"})
    assert dl.status_code == 200
    assert b"BYTES" in dl.content


@smk("SMK-WSP-14")
async def test_git_state_log(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    r = await authed_client.get(f"/v1/workspaces/{wid}/log")
    assert r.status_code == 200, r.text


@smk("SMK-WSP-16", status="partial")
async def test_pause_resume(authed_client, unique_suffix, tmp_path):
    # Workspace-level pause/resume is reserved in v1 (501); the documented
    # contract is the clean not-implemented envelope. Session-level pause/
    # resume is exercised in the agents area.
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    p = await authed_client.post(f"/v1/workspaces/{wid}/pause")
    assert p.status_code == 501, p.text
    assert p.json()["extensions"]["error"] == "not_implemented"


@smk("SMK-WSP-17")
async def test_diagnostic(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    r = await authed_client.post(
        f"/v1/workspaces/{wid}/diagnostic", json={"command": "echo hi", "timeout_seconds": 30}
    )
    assert r.status_code in (200, 201), r.text


@smk("SMK-WSP-10")
async def test_tools_via_agent(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    sc = f"scripted:wsp10-{unique_suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=sc,
        rules=[
            Rule(when_tool_offered="write", when_tool_result=False,
                 emit_tool="workspace__write", emit_args={"path": "agent.txt", "content": "VIA-TOOL"}),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    assert (await wait_terminal(authed_client, sid)).get("status") == "ended"
    rd = await authed_client.get(f"/v1/workspaces/{wid}/files/read", params={"path": "agent.txt", "encoding": "text"})
    assert "VIA-TOOL" in rd.json()["content"]


@smk("SMK-WSP-15")
async def test_two_agents_share_files(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    # producer writes report.txt
    prod = await make_scripted_agent(
        authed_client, registry, base_url, suffix=f"p{unique_suffix}", scenario=f"scripted:wsp15p-{unique_suffix}",
        rules=[
            Rule(when_tool_offered="write", when_tool_result=False,
                 emit_tool="workspace__write", emit_args={"path": "report.txt", "content": "PRODUCED"}),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    sid1 = await start_agent_session(authed_client, workspace_id=wid, agent_id=prod["agent_id"])
    assert (await wait_terminal(authed_client, sid1)).get("status") == "ended"
    # reviewer reads it
    rev = await make_scripted_agent(
        authed_client, registry, base_url, suffix=f"r{unique_suffix}", scenario=f"scripted:wsp15r-{unique_suffix}",
        rules=[
            Rule(when_tool_offered="read", when_tool_result=False,
                 emit_tool="workspace__read", emit_args={"path": "report.txt"}),
            Rule(when_tool_result=True, emit_text="reviewed"),
        ],
    )
    sid2 = await start_agent_session(authed_client, workspace_id=wid, agent_id=rev["agent_id"])
    assert (await wait_terminal(authed_client, sid2)).get("status") == "ended"
    # the producer's file is present in the shared workspace
    rd = await authed_client.get(f"/v1/workspaces/{wid}/files/read", params={"path": "report.txt", "encoding": "text"})
    assert "PRODUCED" in rd.json()["content"]


def _docker_names(kind: str, name: str) -> list[str]:
    """Return docker objects (containers or volumes) matching ``name``.

    Returns an empty list when docker is not on PATH so the leak check
    degrades to a no-op bonus rather than failing the test on hosts where
    the server reaches docker over a socket the test runner cannot.
    """
    if shutil.which("docker") is None:
        return []
    args = (
        ["docker", "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"]
        if kind == "container"
        else ["docker", "volume", "ls", "--filter", f"name={name}", "--format", "{{.Name}}"]
    )
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


@smk("SMK-WSP-12")
@requires("workspace:container")
async def test_container_backend(authed_client, unique_suffix):
    """Materialise a workspace on the Docker container backend, prove file
    + exec work *inside the container*, and tear it down with no leaks.

    The server generates the workspace id (``ws-<hex>``) and names the
    container/volume after THAT id, so we never send an explicit id and use
    only the id returned by ``POST /v1/workspaces`` for every later call.
    """
    wp = f"wpc-{unique_suffix}"
    tpl = f"tplc-{unique_suffix}"
    wid: str | None = None

    rp = await authed_client.post(
        "/v1/workspace_providers",
        json={
            "id": wp,
            "provider": "container",
            "config": {
                "kind": "container",
                "runtime": "docker",
                "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
                "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"},
            },
        },
    )
    assert rp.status_code in (200, 201), rp.text

    rt = await authed_client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl,
            "description": "smk container backend",
            "provider_id": wp,
            "backend": {"kind": "container", "image": "primer/workspace-runtime:1.0"},
        },
    )
    assert rt.status_code in (200, 201), rt.text

    try:
        # No explicit id: the server generates ws-<hex> and names the
        # container/volume after it.
        rw = await authed_client.post("/v1/workspaces", json={"template_id": tpl})
        assert rw.status_code in (200, 201), rw.text
        wid = rw.json()["id"]
        assert wid and wid.startswith("ws-"), wid

        # Poll until the container is running (real backend pull/boot timing).
        phase = None
        for _ in range(90):
            got = await authed_client.get(f"/v1/workspaces/{wid}")
            assert got.status_code == 200, got.text
            phase = got.json().get("phase")
            if phase == "running":
                break
            assert phase not in ("failed", "error"), got.text
            await asyncio.sleep(1.0)
        assert phase == "running", f"workspace never reached running: phase={phase!r}"

        # 1) File write+read round-trip, proving the file surface targets the
        #    container's /workspace volume.
        marker = f"CONTAINER-MARKER-{unique_suffix}"
        w = await authed_client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "marker.txt"},
            json={"content": marker, "encoding": "text"},
        )
        assert w.status_code == 204, w.text
        rd = await authed_client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": "marker.txt", "encoding": "text"},
        )
        assert rd.status_code == 200, rd.text
        assert rd.json()["content"] == marker

        # 2) Exec inside the container. whoami exits 1 in this image (uid 1000
        #    has no passwd entry), so use echo/pwd which exit 0. The file we
        #    wrote above must also be visible to the shell, proving the file
        #    API and exec share the same container filesystem.
        diag = await authed_client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": "echo EXEC-OK; pwd; cat marker.txt", "timeout_seconds": 30},
        )
        assert diag.status_code in (200, 201), diag.text
        body = diag.json()
        assert body["exit_code"] == 0, body
        assert "EXEC-OK" in body["stdout"], body
        assert "/workspace" in body["stdout"], body
        assert marker in body["stdout"], body
    finally:
        # 3) Teardown: DELETE removes the container + volume. Assert the API
        #    contract (204 + GET 404) and, as a bonus, that docker shows no
        #    leaked container/volume named after the workspace id.
        if wid is not None:
            d = await authed_client.delete(f"/v1/workspaces/{wid}")
            assert d.status_code in (204, 404), d.text
            gone = await authed_client.get(f"/v1/workspaces/{wid}")
            assert gone.status_code == 404, gone.text
            assert _docker_names("container", f"workspace-{wid}") == []
            assert _docker_names("volume", f"workspace-{wid}-data") == []
        await authed_client.delete(f"/v1/workspace_templates/{tpl}")
        await authed_client.delete(f"/v1/workspace_providers/{wp}")


async def _make_container_workspace(authed_client, unique_suffix):
    """Create a container provider + template + workspace; return (wp, tpl, wid).

    Waits up to 90 s for the container to reach the ``running`` phase.
    Raises AssertionError when the workspace does not reach running.
    """
    wp = f"wpc-gs-{unique_suffix}"
    tpl = f"tplc-gs-{unique_suffix}"

    rp = await authed_client.post(
        "/v1/workspace_providers",
        json={
            "id": wp,
            "provider": "container",
            "config": {
                "kind": "container",
                "runtime": "docker",
                "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
                "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"},
            },
        },
    )
    assert rp.status_code in (200, 201), rp.text

    rt = await authed_client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl,
            "description": "smk container graph/agent session",
            "provider_id": wp,
            "backend": {"kind": "container", "image": "primer/workspace-runtime:1.0"},
        },
    )
    assert rt.status_code in (200, 201), rt.text

    rw = await authed_client.post("/v1/workspaces", json={"template_id": tpl})
    assert rw.status_code in (200, 201), rw.text
    wid = rw.json()["id"]
    assert wid and wid.startswith("ws-"), wid

    phase = None
    for _ in range(90):
        got = await authed_client.get(f"/v1/workspaces/{wid}")
        assert got.status_code == 200, got.text
        phase = got.json().get("phase")
        if phase == "running":
            break
        assert phase not in ("failed", "error"), got.text
        await asyncio.sleep(1.0)
    assert phase == "running", f"container workspace never reached running: phase={phase!r}"
    return wp, tpl, wid


@smk("SMK-WSP-12")
@requires("workspace:container")
async def test_container_backend_graph_session(authed_client, mock_llm, unique_suffix):
    """Graph-bound session on a container workspace runs to clean terminal.

    Proves: create_session returns 201 (not 422/500), the session reaches
    ``ended`` status, and no /errors/internal appears in last_error. Also
    verifies the graph state directory was committed to the pod via diagnostic.
    """
    registry, base_url = mock_llm
    sc = f"scripted:cgs-{unique_suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url,
        suffix=f"cgs-{unique_suffix}",
        scenario=sc,
        rules=[Rule(emit_text="done")],
    )

    wp, tpl, wid = await _make_container_workspace(authed_client, unique_suffix)
    gid = await make_graph(
        authed_client,
        suffix=f"cgs-{unique_suffix}",
        nodes=[
            {"kind": "begin", "id": "start"},
            {"kind": "agent", "id": "step", "agent_id": agent["agent_id"],
             "input_template": "go"},
            {"kind": "end", "id": "done", "output_template": "{{ nodes.step.text }}"},
        ],
        edges=[
            {"kind": "static", "from_node": "start", "to_node": "step"},
            {"kind": "static", "from_node": "step", "to_node": "done"},
        ],
    )

    try:
        r = await authed_client.post(
            f"/v1/workspaces/{wid}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": gid},
                "auto_start": True,
            },
        )
        # The key proof: must be 201, not 422/500
        assert r.status_code == 201, (
            f"graph session create should be 201; got {r.status_code}: {r.text}"
        )
        sid = r.json()["id"]

        # Poll to terminal -- container sessions need extra time
        final = await wait_terminal(authed_client, sid, timeout_s=120.0, interval_s=1.0)
        assert final.get("status") == "ended", (
            f"container graph session did not reach ended: {final}"
        )
        last_err = final.get("last_error")
        if last_err:
            assert "/errors/internal" not in str(last_err.get("type", "")), (
                f"container graph session has /errors/internal: {last_err}"
            )

        # Verify graph state persisted inside the pod.
        # The executor names the state subdir after the WorkspaceSession id
        # (the graph_session_id passed to WorkspaceGraphExecutor is session.id),
        # not the graph row id -- so assert sid appears in .state/graphs.
        diag = await authed_client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": "ls .state/graphs", "timeout_seconds": 30},
        )
        assert diag.status_code in (200, 201), diag.text
        assert diag.json()["exit_code"] == 0, diag.json()
        assert diag.json()["stdout"].strip(), (
            "no graph state dirs found in pod .state/graphs after graph session "
            "(stdout empty)"
        )
        assert sid in diag.json()["stdout"], (
            f"session state dir {sid!r} missing from pod .state/graphs: "
            f"{diag.json()['stdout']!r}"
        )
    finally:
        if wid is not None:
            d = await authed_client.delete(f"/v1/workspaces/{wid}")
            assert d.status_code in (204, 404), d.text
            gone = await authed_client.get(f"/v1/workspaces/{wid}")
            assert gone.status_code == 404, gone.text
            assert _docker_names("container", f"workspace-{wid}") == []
            assert _docker_names("volume", f"workspace-{wid}-data") == []
        await authed_client.delete(f"/v1/workspace_templates/{tpl}")
        await authed_client.delete(f"/v1/workspace_providers/{wp}")
        await authed_client.delete(f"/v1/graphs/{gid}")
        await authed_client.delete(f"/v1/agents/{agent['agent_id']}")
        await authed_client.delete(f"/v1/llm_providers/{agent['provider_id']}")


@smk("SMK-WSP-12")
@requires("workspace:container")
async def test_container_backend_agent_session(authed_client, mock_llm, unique_suffix):
    """Agent-bound session on a container workspace runs to clean terminal.

    Proves: create_session + commit work on the pod. The session reaches
    ``ended`` status with no /errors/internal in last_error.
    """
    registry, base_url = mock_llm
    sc = f"scripted:cas-{unique_suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url,
        suffix=f"cas-{unique_suffix}",
        scenario=sc,
        rules=[Rule(emit_text="done")],
    )

    wp, tpl, wid = await _make_container_workspace(authed_client, unique_suffix)

    try:
        sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])

        # Poll to terminal -- container sessions need extra time
        final = await wait_terminal(authed_client, sid, timeout_s=120.0, interval_s=1.0)
        assert final.get("status") == "ended", (
            f"container agent session did not reach ended: {final}"
        )
        last_err = final.get("last_error")
        if last_err:
            assert "/errors/internal" not in str(last_err.get("type", "")), (
                f"container agent session has /errors/internal: {last_err}"
            )
    finally:
        if wid is not None:
            d = await authed_client.delete(f"/v1/workspaces/{wid}")
            assert d.status_code in (204, 404), d.text
            gone = await authed_client.get(f"/v1/workspaces/{wid}")
            assert gone.status_code == 404, gone.text
            assert _docker_names("container", f"workspace-{wid}") == []
            assert _docker_names("volume", f"workspace-{wid}-data") == []
        await authed_client.delete(f"/v1/workspace_templates/{tpl}")
        await authed_client.delete(f"/v1/workspace_providers/{wp}")
        await authed_client.delete(f"/v1/agents/{agent['agent_id']}")
        await authed_client.delete(f"/v1/llm_providers/{agent['provider_id']}")


def _kubectl_names(kind: str, name: str, namespace: str) -> list[str]:
    """Return matching object names of ``kind`` in ``namespace`` (or []).

    Used to assert the K8s backend leaves no Secret/Service/StatefulSet/PVC
    behind after a workspace is destroyed. Mirrors ``_docker_names`` for the
    container backend. Returns [] when kubectl is unavailable or errors so a
    missing CLI does not masquerade as a leak.
    """
    try:
        out = subprocess.run(
            ["kubectl", "get", kind, "-n", namespace,
             "-o", "jsonpath={.items[*].metadata.name}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [n for n in out.stdout.split() if n == name]


@smk("SMK-WSP-13")
@requires("workspace:kubernetes")
async def test_kubernetes_backend(authed_client, unique_suffix):
    """Materialise a workspace on the Kubernetes (k3s) backend, prove file +
    exec work *inside the pod* via in-cluster service DNS, and tear it down
    with no leaked K8s objects.

    Topology: this exercises the ``in_cluster`` reachability mode, which only
    works when the primer platform itself runs inside the cluster. The test
    therefore targets the in-cluster platform's API; point the e2e harness at
    it via ``PRIMER_E2E_BASE_URL`` (e.g. the NodePort of the in-cluster
    ``primer-api`` Service). The runtime image must be pullable by the cluster
    (a cluster-local registry tag); override the default via
    ``PRIMER_K8S_RUNTIME_IMAGE``. The workspace namespace defaults to
    ``primer-workspaces`` and can be overridden via ``PRIMER_K8S_NAMESPACE``.

    Like SMK-WSP-12 the server generates the workspace id (``ws-<hex>``) and
    names every K8s object ``primer-ws-<id>``; we never send an explicit id and
    use only the id returned by ``POST /v1/workspaces``.
    """
    import os

    namespace = os.environ.get("PRIMER_K8S_NAMESPACE", "primer-workspaces")
    runtime_image = _k8s_runtime_image("1.0")
    wp = f"wpk-{unique_suffix}"
    tpl = f"tplk-{unique_suffix}"
    wid: str | None = None
    obj_name: str | None = None

    rp = await authed_client.post(
        "/v1/workspace_providers",
        json={
            "id": wp,
            "provider": "kubernetes",
            "config": {
                "kind": "kubernetes",
                "connection": {"kind": "in_cluster"},
                "namespace": namespace,
                "reachability": {"kind": "in_cluster"},
            },
        },
    )
    assert rp.status_code in (200, 201), rp.text

    rt = await authed_client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl,
            "description": "smk k8s backend",
            "provider_id": wp,
            "backend": {
                "kind": "kubernetes",
                "image": runtime_image,
                # The runtime image's ENTRYPOINT launches the server; the
                # backend defaults `command` to `sleep infinity` when no
                # entrypoint is given, so set it explicitly.
                "entrypoint": ["python", "-m", "primer_runtime.server"],
                "pvc_size": "1Gi",
            },
        },
    )
    assert rt.status_code in (200, 201), rt.text

    try:
        # No explicit id: the server generates ws-<hex> and names the K8s
        # objects primer-ws-<id>.
        rw = await authed_client.post("/v1/workspaces", json={"template_id": tpl})
        assert rw.status_code in (200, 201), rw.text
        body = rw.json()
        wid = body["id"]
        assert wid and wid.startswith("ws-"), wid
        obj_name = body["runtime_meta"]["k8s_object_name"]
        assert obj_name == f"primer-ws-{wid}", body

        # Poll until the pod is running (real image pull + StatefulSet boot).
        phase = None
        for _ in range(120):
            got = await authed_client.get(f"/v1/workspaces/{wid}")
            assert got.status_code == 200, got.text
            phase = got.json().get("phase")
            if phase == "running":
                break
            assert phase not in ("failed", "error"), got.text
            await asyncio.sleep(1.0)
        assert phase == "running", f"workspace never reached running: phase={phase!r}"

        # 1) File write+read round-trip, proving the file surface targets the
        #    pod's /workspace PVC via the in-cluster runtime WebSocket.
        marker = f"K8S-MARKER-{unique_suffix}"
        w = await authed_client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "marker.txt"},
            json={"content": marker, "encoding": "text"},
        )
        assert w.status_code == 204, w.text
        rd = await authed_client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": "marker.txt", "encoding": "text"},
        )
        assert rd.status_code == 200, rd.text
        assert rd.json()["content"] == marker

        # 2) Exec inside the pod. echo/pwd exit 0; the file written above must
        #    be visible to the shell, proving file API and exec share the pod
        #    filesystem.
        diag = await authed_client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": "echo EXEC-OK; pwd; cat marker.txt", "timeout_seconds": 30},
        )
        assert diag.status_code in (200, 201), diag.text
        db = diag.json()
        assert db["exit_code"] == 0, db
        assert "EXEC-OK" in db["stdout"], db
        assert "/workspace" in db["stdout"], db
        assert marker in db["stdout"], db
    finally:
        # 3) Teardown: DELETE removes the StatefulSet + Service + Secret + PVC.
        #    Assert the API contract (204 + GET 404) and that kubectl shows no
        #    leaked objects named after the workspace.
        if wid is not None:
            d = await authed_client.delete(f"/v1/workspaces/{wid}")
            assert d.status_code in (204, 404), d.text
            gone = await authed_client.get(f"/v1/workspaces/{wid}")
            assert gone.status_code == 404, gone.text
            if obj_name is not None:
                # The StatefulSet controller deletes the pod; allow a moment
                # for the objects to disappear before asserting no leaks.
                for _ in range(30):
                    leaks = (
                        _kubectl_names("statefulset", obj_name, namespace)
                        + _kubectl_names("service", obj_name, namespace)
                        + _kubectl_names("secret", obj_name, namespace)
                        + _kubectl_names("pvc", f"ws-{obj_name}-0", namespace)
                    )
                    if not leaks:
                        break
                    await asyncio.sleep(1.0)
                assert leaks == [], f"leaked K8s objects: {leaks}"
        await authed_client.delete(f"/v1/workspace_templates/{tpl}")
        await authed_client.delete(f"/v1/workspace_providers/{wp}")


@smk("SMK-WSP-13")
@requires("workspace:kubernetes")
async def test_kubernetes_backend_graph_session(authed_client, unique_suffix):
    """Graph-bound session on a KUBERNETES workspace runs to a clean terminal.

    The k8s lane of StateRepo parity: proves the in-cluster platform +
    runtime image speak the 1.1 state protocol end to end. The session is
    created (201, not 422/500), reaches ``ended``, and its node state is
    committed to the pod's ``.state/graphs/<session_id>`` git tree.

    Uses a ``begin -> end`` graph with NO agent node, so no LLM call is made.
    That is deliberate: the in-cluster platform cannot reach the host-side
    mock LLM, and the proof here is purely that ``create_session`` plus the
    per-node commits roundtrip through the real pod over the runtime
    WebSocket. The runtime image must speak protocol >=1.1 (override via
    ``PRIMER_K8S_RUNTIME_IMAGE``; defaults to the cluster-local 1.1 tag).
    """
    import os

    namespace = os.environ.get("PRIMER_K8S_NAMESPACE", "primer-workspaces")
    runtime_image = _k8s_runtime_image("1.1")
    wp = f"wpkg-{unique_suffix}"
    tpl = f"tplkg-{unique_suffix}"
    wid: str | None = None

    rp = await authed_client.post(
        "/v1/workspace_providers",
        json={
            "id": wp,
            "provider": "kubernetes",
            "config": {
                "kind": "kubernetes",
                "connection": {"kind": "in_cluster"},
                "namespace": namespace,
                "reachability": {"kind": "in_cluster"},
            },
        },
    )
    assert rp.status_code in (200, 201), rp.text
    rt = await authed_client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl,
            "description": "smk k8s graph session",
            "provider_id": wp,
            "backend": {
                "kind": "kubernetes",
                "image": runtime_image,
                "entrypoint": ["python", "-m", "primer_runtime.server"],
                "pvc_size": "1Gi",
            },
        },
    )
    assert rt.status_code in (200, 201), rt.text

    gid = await make_graph(
        authed_client,
        suffix=f"kgs-{unique_suffix}",
        nodes=[
            {"kind": "begin", "id": "start"},
            {"kind": "end", "id": "done", "output_template": "ok"},
        ],
        edges=[
            {"kind": "static", "from_node": "start", "to_node": "done"},
        ],
    )

    try:
        rw = await authed_client.post("/v1/workspaces", json={"template_id": tpl})
        assert rw.status_code in (200, 201), rw.text
        wid = rw.json()["id"]

        # Wait for the pod to be running (real image pull + StatefulSet boot).
        phase = None
        for _ in range(120):
            got = await authed_client.get(f"/v1/workspaces/{wid}")
            assert got.status_code == 200, got.text
            phase = got.json().get("phase")
            if phase == "running":
                break
            assert phase not in ("failed", "error"), got.text
            await asyncio.sleep(1.0)
        assert phase == "running", f"workspace never reached running: phase={phase!r}"

        r = await authed_client.post(
            f"/v1/workspaces/{wid}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": gid},
                "auto_start": True,
            },
        )
        # The key proof: must be 201, not 422/500.
        assert r.status_code == 201, (
            f"k8s graph session create should be 201; got {r.status_code}: {r.text}"
        )
        sid = r.json()["id"]

        final = await wait_terminal(authed_client, sid, timeout_s=120.0, interval_s=1.0)
        assert final.get("status") == "ended", (
            f"k8s graph session did not reach ended: {final}"
        )
        last_err = final.get("last_error")
        if last_err:
            assert "/errors/internal" not in str(last_err.get("type", "")), (
                f"k8s graph session has /errors/internal: {last_err}"
            )

        # Verify the graph node state was committed inside the k8s pod.
        diag = await authed_client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": "ls .state/graphs", "timeout_seconds": 30},
        )
        assert diag.status_code in (200, 201), diag.text
        assert diag.json()["exit_code"] == 0, diag.json()
        assert sid in diag.json()["stdout"], (
            f"session state dir {sid!r} missing from pod .state/graphs: "
            f"{diag.json()['stdout']!r}"
        )
    finally:
        if wid is not None:
            d = await authed_client.delete(f"/v1/workspaces/{wid}")
            assert d.status_code in (204, 404), d.text
            gone = await authed_client.get(f"/v1/workspaces/{wid}")
            assert gone.status_code == 404, gone.text
        await authed_client.delete(f"/v1/workspace_templates/{tpl}")
        await authed_client.delete(f"/v1/workspace_providers/{wp}")
        await authed_client.delete(f"/v1/graphs/{gid}")


@smk("SMK-WSP-13")
@requires("workspace:kubernetes")
async def test_kubernetes_backend_gateway(authed_client, unique_suffix):
    """Approach A: a host-side platform reaches in-cluster runtime pods via a
    Gateway API HTTPRoute the backend auto-creates.

    Topology: the platform runs on the host (the default e2e target,
    localhost:8765) and talks to the apiserver via a kubeconfig connection;
    runtime pods are reached through traefik's Gateway + a wildcard DNS entry
    (``*.<PRIMER_K8S_WS_DOMAIN, default ws.local>`` -> node IP). Requires the
    Gateway + wildcard-DNS infra (see the plan's Task 6). Override the dial
    port via ``PRIMER_K8S_GATEWAY_PORT`` (default 32045, traefik web nodePort)
    and the runtime image via ``PRIMER_K8S_RUNTIME_IMAGE``.

    Like the in_cluster variant the server generates the workspace id and names
    every K8s object ``primer-ws-<id>``; we additionally assert the per-
    workspace HTTPRoute is created on materialise and deleted on teardown.
    """
    import os

    namespace = os.environ.get("PRIMER_K8S_NAMESPACE", "primer-workspaces")
    runtime_image = _k8s_runtime_image("1.0")
    gateway_port = int(os.environ.get("PRIMER_K8S_GATEWAY_PORT", "32045"))
    ws_domain = os.environ.get("PRIMER_K8S_WS_DOMAIN", "ws.local")
    kubeconfig = os.environ.get("KUBECONFIG") or os.path.expanduser("~/.kube/config")

    wp = f"wpkg-{unique_suffix}"
    tpl = f"tplkg-{unique_suffix}"
    wid: str | None = None
    obj_name: str | None = None

    rp = await authed_client.post(
        "/v1/workspace_providers",
        json={
            "id": wp,
            "provider": "kubernetes",
            "config": {
                "kind": "kubernetes",
                "connection": {"kind": "kubeconfig", "path": kubeconfig},
                "namespace": namespace,
                "reachability": {
                    "kind": "gateway_httproute",
                    "scheme": "ws",
                    "external_port": gateway_port,
                    "gateway": {"name": "primer-gw", "namespace": "primer-gateway"},
                    "routing": {
                        "kind": "hostname",
                        "hostname_template": "{workspace_id}." + ws_domain,
                    },
                },
            },
        },
    )
    assert rp.status_code in (200, 201), rp.text

    rt = await authed_client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl,
            "description": "smk k8s gateway backend",
            "provider_id": wp,
            "backend": {
                "kind": "kubernetes",
                "image": runtime_image,
                "entrypoint": ["python", "-m", "primer_runtime.server"],
                "pvc_size": "1Gi",
            },
        },
    )
    assert rt.status_code in (200, 201), rt.text

    try:
        rw = await authed_client.post("/v1/workspaces", json={"template_id": tpl})
        assert rw.status_code in (200, 201), rw.text
        body = rw.json()
        wid = body["id"]
        assert wid and wid.startswith("ws-"), wid
        obj_name = body["runtime_meta"]["k8s_object_name"]
        assert obj_name == f"primer-ws-{wid}", body

        # The per-workspace HTTPRoute must exist once the workspace is created.
        assert _kubectl_names("httproute", obj_name, namespace) == [obj_name]

        phase = None
        for _ in range(120):
            got = await authed_client.get(f"/v1/workspaces/{wid}")
            assert got.status_code == 200, got.text
            phase = got.json().get("phase")
            if phase == "running":
                break
            assert phase not in ("failed", "error"), got.text
            await asyncio.sleep(1.0)
        assert phase == "running", f"workspace never reached running: phase={phase!r}"

        marker = f"K8S-GW-MARKER-{unique_suffix}"
        w = await authed_client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "marker.txt"},
            json={"content": marker, "encoding": "text"},
        )
        assert w.status_code == 204, w.text
        rd = await authed_client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": "marker.txt", "encoding": "text"},
        )
        assert rd.status_code == 200, rd.text
        assert rd.json()["content"] == marker

        diag = await authed_client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": "echo EXEC-OK; pwd; cat marker.txt", "timeout_seconds": 30},
        )
        assert diag.status_code in (200, 201), diag.text
        db = diag.json()
        assert db["exit_code"] == 0, db
        assert "EXEC-OK" in db["stdout"], db
        assert "/workspace" in db["stdout"], db
        assert marker in db["stdout"], db
    finally:
        if wid is not None:
            d = await authed_client.delete(f"/v1/workspaces/{wid}")
            assert d.status_code in (204, 404), d.text
            gone = await authed_client.get(f"/v1/workspaces/{wid}")
            assert gone.status_code == 404, gone.text
            if obj_name is not None:
                leaks: list[str] = []
                for _ in range(30):
                    leaks = (
                        _kubectl_names("statefulset", obj_name, namespace)
                        + _kubectl_names("service", obj_name, namespace)
                        + _kubectl_names("secret", obj_name, namespace)
                        + _kubectl_names("httproute", obj_name, namespace)
                        + _kubectl_names("pvc", f"ws-{obj_name}-0", namespace)
                    )
                    if not leaks:
                        break
                    await asyncio.sleep(1.0)
                assert leaks == [], f"leaked K8s objects: {leaks}"
        await authed_client.delete(f"/v1/workspace_templates/{tpl}")
        await authed_client.delete(f"/v1/workspace_providers/{wp}")
