"""Cookbook recipe (CLI path): sandboxed code interpreter, driven by primectl.

The ``primectl``-driven sibling of ``test_cookbook_code_interpreter``. Where that
test drives the recipe over the REST API, this one performs every step with the
exact ``primectl`` commands the migrated doc shows, so the doc's "Via the CLI"
path is a tested contract:

  * ``primectl create -f`` the container workspace provider + template, the
    scripted LLM provider, and the ``code-runner`` agent;
  * ``primectl create workspace --set template_id=`` the sandbox, polled to
    ``running`` with ``primectl get workspace``;
  * ``primectl session run`` the untrusted snippet to terminal; and
  * ``primectl workspace files get`` each produced file back through the
    workspace file API (which targets the container's ``/workspace`` volume).

The same isolation outcome the API test asserts is checked back: the snippet ran
INSIDE the sandbox (``out.txt`` is ``42``), the in-container hostname differs
from the host (UTS namespace), and the host docker socket is absent inside the
sandbox (mount isolation).

Capability-gated on ``workspace:container`` so it SKIPS cleanly where docker is
absent. Agent behaviour is scripted (deterministic mock LLM).

Recipe: primerhq.github.io/docs_source/cookbook/code-interpreter.md
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = [requires("workspace:container")]

_HOST_HOSTNAME = socket.gethostname()

_SNIPPET = (
    "import socket, os\n"
    "print('RESULT', 6 * 7)\n"
    "open('/workspace/out.txt', 'w').write(str(6 * 7))\n"
    "open('/workspace/sandbox_host.txt', 'w').write(socket.gethostname())\n"
    "open('/workspace/host_sock.txt', 'w').write(\n"
    "    str(os.path.exists('/var/run/docker.sock')))\n"
)


def _docker_names(kind: str, name: str) -> list[str]:
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


def _wait_running(pc: Primectl, wid: str, *, timeout_s: float = 150.0) -> None:
    deadline = time.monotonic() + timeout_s
    phase = None
    while time.monotonic() < deadline:
        row = pc.run("get", "workspace", wid, "-r", "-o", "json").json()
        phase = row.get("phase")
        if phase == "running":
            return
        assert phase not in ("failed", "error"), row
        time.sleep(1.5)
    raise AssertionError(f"container workspace {wid} never reached running: phase={phase!r}")


@smk("SMK-COOKBOOK-CLI-17")
def test_code_interpreter_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-ci-{sfx}"))

    pid = f"p-ci-cli-{sfx}"
    aid = f"a-ci-cli-{sfx}"
    wp = f"wp-ci-cli-{sfx}"
    tpl = f"tpl-ci-cli-{sfx}"
    scenario = f"scripted:ci-cli-{sfx}"

    # code-runner: write the snippet, exec it, then report. Discriminated on
    # tool-result presence/content (the workspace tools are agent-implicit on a
    # workspace-bound session, so the agent's ``tools`` list stays empty).
    registry.register(scenario, [
        Rule(when_tool_result=False, emit_tool="workspace__write",
             emit_args={"path": "snippet.py", "content": _SNIPPET}),
        Rule(when_tool_result=True, when_last_tool_result_contains="snippet.py",
             emit_tool="workspace__exec",
             emit_args={"command": "python snippet.py",
                        "description": "run the untrusted snippet"}),
        Rule(when_tool_result=True, when_last_tool_result_contains="RESULT",
             emit_text="The snippet computed 42."),
        Rule(when_tool_result=True, emit_text="done"),
    ])

    wid: str | None = None
    try:
        # ---- provider + template + agent (CLI: create -f) ----------------
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "container",
            "config": {
                "kind": "container", "runtime": "docker",
                "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
                "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"},
            },
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "cookbook code interpreter sandbox (cli)",
            "provider_id": wp,
            "backend": {"kind": "container", "image": "primer/workspace-runtime:1.0"},
        }))
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [{"name": scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))
        pc.run("create", "-f", manifest(tmp_path, "agent", "agent", {
            "id": aid, "description": "Runs untrusted code in the sandbox.",
            "model": {"provider_id": pid, "model_name": scenario},
            "tools": [],
            "system_prompt": [
                "You run untrusted user code in the sandbox. Write the snippet "
                "to snippet.py, run `python snippet.py`, and report its stdout."
            ],
        }))

        # ---- materialise the sandbox + wait for running ------------------
        wid = pc.run("create", "workspace", "--set", f"template_id={tpl}").stdout.split("/")[1].split()[0]
        assert wid and wid.startswith("ws-"), wid
        _wait_running(pc, wid)

        # ---- run the snippet to terminal (CLI: session run) --------------
        run = pc.run(
            "session", "run", wid, "--agent", aid,
            "-i", "Run this code: print(6*7)", "--timeout", "200",
        )
        assert "ended: completed" in run.stdout, run.stdout

        # ---- read the produced files back (CLI: workspace files get) -----
        def _read(path: str) -> str:
            return pc.run("workspace", "files", "get", wid, path, "--content").stdout.strip()

        # (a) execution happened INSIDE the sandbox: 6*7 -> 42 in /workspace.
        assert _read("out.txt") == "42", "snippet did not compute 42 in the sandbox"
        # (b) namespace isolation: container hostname differs from the host's.
        sandbox_host = _read("sandbox_host.txt")
        assert sandbox_host and sandbox_host != _HOST_HOSTNAME, (
            f"in-container hostname {sandbox_host!r} should differ from host "
            f"{_HOST_HOSTNAME!r} (namespace isolation)"
        )
        # (c) mount isolation: host docker socket is absent inside the sandbox.
        assert _read("host_sock.txt") == "False", (
            "host docker.sock should be ABSENT inside the sandbox"
        )
    finally:
        if wid is not None:
            pc.run("delete", "workspace", wid, check=False)
            # Clean lifecycle: no leaked docker container/volume after delete.
            assert _docker_names("container", f"workspace-{wid}") == []
            assert _docker_names("volume", f"workspace-{wid}-data") == []
        pc.run("delete", "workspace_template", tpl, check=False)
        pc.run("delete", "workspace_provider", wp, check=False)
        pc.run("delete", "agent", aid, check=False)
        pc.run("delete", "llm_provider", pid, check=False)
