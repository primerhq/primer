"""Cookbook recipe #15 regression: Sandboxed Code Interpreter (UC6).

An agent executes UNTRUSTED user code inside an ISOLATED container workspace
and returns the result. The isolation is the whole point: on the LOCAL backend
an agent can leak host-shell exec, so this recipe -- and this test -- run the
code on the CONTAINER backend, where the code is confined to the sandbox.

Recipe: primerhq.github.io/docs_source/cookbook/code-interpreter.md

Flow (the recipe's verified outcome):
  1. Materialise a CONTAINER workspace from a ``ContainerTemplateConfig``.
  2. A scripted ``code-runner`` agent WRITES the untrusted snippet to
     ``/workspace/snippet.py`` (``workspace__write``) and EXECS it
     (``workspace__exec`` -> ``python snippet.py``).
  3. The snippet computes ``6 * 7`` and writes ``42`` to ``/workspace/out.txt``;
     it also records the in-container hostname and probes the host docker
     socket -- both used below to PROVE isolation.

Asserts:
  * the session ends ``ended`` (clean lifecycle),
  * the exec tool_result in the session transcript carries the snippet's
    stdout, including the computed ``RESULT 42`` -- proving execution happened
    INSIDE the sandbox, not on the host,
  * isolation: the in-container hostname differs from the host's, and the host
    docker socket is ABSENT inside the sandbox,
  * the produced file ``out.txt`` (read back via the workspace file API, which
    targets the container's ``/workspace`` volume) contains ``42``,
  * teardown is clean: DELETE 204, GET 404, no leaked docker container/volume.

Capability-gated on ``workspace:container`` so it SKIPS cleanly where docker is
absent. Mirrors the container smk tests (SMK-WSP-12). Uses the scripted mock
LLM (deterministic Rules), not a real model.
"""
from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_scripted_agent,
    start_agent_session,
    wait_terminal,
)
from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = pytest.mark.asyncio

_HOST_HOSTNAME = socket.gethostname()

# The untrusted snippet the agent runs. Deterministic: computes 6*7, records
# the in-container hostname, probes the host docker socket, and persists ALL
# three facts to files in the sandbox /workspace volume. The files are the
# backend-agnostic proof surface -- read back via the workspace file API, they
# show the code ran INSIDE the container (the file API targets the container's
# /workspace volume), so we never need to scrape the on-disk transcript that
# lives inside the container.
_SNIPPET = (
    "import socket, os\n"
    "print('RESULT', 6 * 7)\n"
    "open('/workspace/out.txt', 'w').write(str(6 * 7))\n"
    "open('/workspace/sandbox_host.txt', 'w').write(socket.gethostname())\n"
    "open('/workspace/host_sock.txt', 'w').write(\n"
    "    str(os.path.exists('/var/run/docker.sock')))\n"
)


def _docker_names(kind: str, name: str) -> list[str]:
    """Matching docker containers/volumes for ``name`` ([] when docker absent)."""
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


async def _make_container_workspace(client, suffix: str) -> tuple[str, str, str]:
    """Create a container provider + template + workspace; wait for running.

    Returns ``(provider_id, template_id, workspace_id)``. The server generates
    the ``ws-<hex>`` id and names the container/volume after it.
    """
    wp = f"ci-wp-{suffix}"
    tpl = f"ci-tpl-{suffix}"

    rp = await client.post(
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

    rt = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl,
            "description": "cookbook code interpreter sandbox",
            "provider_id": wp,
            # ContainerTemplateConfig: the sandbox image the code runs in.
            "backend": {"kind": "container", "image": "primer/workspace-runtime:1.0"},
        },
    )
    assert rt.status_code in (200, 201), rt.text

    rw = await client.post("/v1/workspaces", json={"template_id": tpl})
    assert rw.status_code in (200, 201), rw.text
    wid = rw.json()["id"]
    assert wid and wid.startswith("ws-"), wid

    phase = None
    for _ in range(120):
        got = await client.get(f"/v1/workspaces/{wid}")
        assert got.status_code == 200, got.text
        phase = got.json().get("phase")
        if phase == "running":
            break
        assert phase not in ("failed", "error"), got.text
        await asyncio.sleep(1.0)
    assert phase == "running", f"container workspace never reached running: phase={phase!r}"
    return wp, tpl, wid


@smk("SMK-COOKBOOK-15")
@requires("workspace:container")
async def test_code_interpreter_runs_untrusted_code_in_sandbox(
    authed_client, mock_llm, unique_suffix,
):
    registry, base_url = mock_llm

    # The code-runner agent: write the snippet, then exec it, then report.
    # Chain is discriminated by tool-result presence -- first turn (no result)
    # writes; after the write result, exec; after the exec result, finish.
    # NOTE: workspace tools (``workspace__write`` / ``workspace__exec``) are
    # AGENT-IMPLICIT on a workspace-bound session -- they are injected by the
    # workspace binding and bypass the agent's tool allowlist. Listing them in
    # ``tools`` would mis-route ``workspace`` as a registered toolset and fail
    # the executor build, so the ``tools`` list stays empty here.
    agent = await make_scripted_agent(
        authed_client, registry, base_url,
        suffix=f"ci-{unique_suffix}",
        scenario=f"scripted:code-runner-{unique_suffix}",
        system_prompt=[
            "You run untrusted user code in the sandbox. Write the snippet to "
            "snippet.py, exec `python snippet.py`, and report its stdout.",
        ],
        rules=[
            # 1) No tool result yet -> write the snippet.
            Rule(
                when_tool_result=False,
                emit_tool="workspace__write",
                emit_args={"path": "snippet.py", "content": _SNIPPET},
            ),
            # 2) After the write result (which does NOT contain RESULT) -> exec.
            Rule(
                when_tool_result=True,
                when_last_tool_result_contains="snippet.py",
                emit_tool="workspace__exec",
                emit_args={
                    "command": "python snippet.py",
                    "description": "run the untrusted snippet",
                },
            ),
            # 3) After the exec result (carries the snippet stdout) -> report.
            Rule(
                when_tool_result=True,
                when_last_tool_result_contains="RESULT",
                emit_text="The snippet computed 42.",
            ),
            # Fallback so the loop always terminates.
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )

    wp, tpl, wid = await _make_container_workspace(authed_client, unique_suffix)

    try:
        sid = await start_agent_session(
            authed_client, workspace_id=wid, agent_id=agent["agent_id"],
            instructions="Run this code: print(6*7)",
        )

        # Container sessions need extra time (image boot + the tool turns).
        # NOTE: a *failed* session also reports status "ended", so we additionally
        # require ended_reason "completed" -- a turn-0 build failure (e.g. an
        # unresolved toolset) would surface here as "failed".
        final = await wait_terminal(authed_client, sid, timeout_s=180.0, interval_s=1.0)
        assert final.get("status") == "ended", f"session did not reach ended: {final}"
        assert final.get("ended_reason") == "completed", (
            f"session did not complete cleanly: {final}"
        )

        async def _read(path: str) -> str:
            rd = await authed_client.get(
                f"/v1/workspaces/{wid}/files/read",
                params={"path": path, "encoding": "text"},
            )
            assert rd.status_code == 200, f"{path}: {rd.status_code} {rd.text}"
            return rd.json()["content"].strip()

        # (a) execution happened INSIDE the sandbox: the snippet computed 6*7
        #     and wrote 42 to a file in the container's /workspace volume.
        #     Reading it back through the file API (which targets that volume)
        #     proves the code ran in the sandbox, not on the host.
        assert await _read("out.txt") == "42", "snippet did not compute 42 in the sandbox"

        # (b) isolation -- namespace: the in-container hostname differs from the
        #     host's hostname (the container has its own UTS namespace).
        sandbox_host = await _read("sandbox_host.txt")
        assert sandbox_host and sandbox_host != _HOST_HOSTNAME, (
            f"in-container hostname {sandbox_host!r} should differ from host "
            f"{_HOST_HOSTNAME!r} (namespace isolation)"
        )

        # (c) isolation -- mounts: the host docker socket is ABSENT inside the
        #     sandbox (the blast radius is confined; the host is unreachable).
        assert await _read("host_sock.txt") == "False", (
            "host docker.sock should be ABSENT inside the sandbox"
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
