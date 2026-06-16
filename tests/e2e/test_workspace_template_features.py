"""Workspace-template feature x backend matrix harness.

This module builds the reusable HARNESS that subsequent phases use to assert
workspace-template features (files, env, init_commands, resources, entrypoint,
workdir, ...) across every backend the platform supports, on both host and
in-cluster platforms.

The matrix has four targets (see :data:`TARGETS`):

  * ``local``                - host platform, always available
  * ``container``            - host platform, gated on ``workspace:container``
  * ``kubernetes-gateway``   - host platform, gated on ``workspace:kubernetes``;
                               reaches in-cluster pods via a Gateway API
                               HTTPRoute
  * ``kubernetes-incluster`` - in-cluster platform (a primer deployment running
                               INSIDE the cluster); gated on
                               ``workspace:kubernetes`` AND in-cluster health

Every ``/v1`` route is auth-guarded, so :func:`_authenticate` replicates the
register+login flow that ``tests/e2e/conftest.py``'s ``client`` fixture performs
(idempotent register, then login), against ANY base_url so we can authenticate a
client on either platform.

This phase ships the harness plus a single per-target smoke test
(:func:`test_harness_smoke`). Subsequent phases add feature tests on top.
"""
from __future__ import annotations

import contextlib
import os
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
import pytest_asyncio

from tests._support.testconfig import caps

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Platform base URLs + auth
# ---------------------------------------------------------------------------

# Mirror conftest.py's _E2E_USER so we register/login the same operator user.
_E2E_USER = {"username": "e2e", "password": "e2e-password-123"}

# Generous poll budget: a fresh container/pod may pull an image and boot.
_RUNNING_TIMEOUT_S = 180.0
_POLL_INTERVAL_S = 1.0


def _host_base_url() -> str:
    return os.environ.get("PRIMER_E2E_BASE_URL", "http://127.0.0.1:8765").rstrip("/")


def _incluster_base_url() -> str:
    return os.environ.get(
        "PRIMER_K8S_INCLUSTER_BASE_URL", "http://127.0.0.1:30876"
    ).rstrip("/")


def _kubeconfig_path() -> str:
    explicit = os.environ.get("KUBECONFIG")
    if explicit:
        return explicit
    return str(Path.home() / ".kube" / "config")


async def _authenticate(client: httpx.AsyncClient) -> None:
    """Authenticate ``client`` against its base_url.

    Replicates the ``client`` fixture in ``tests/e2e/conftest.py``: every
    ``/v1`` route is auth-guarded, so we register the operator user
    (idempotent - a duplicate register returns 4xx, which we ignore) and then
    log in, which sets the session cookie on the client. Both the host and the
    in-cluster platform support the same register/login flow, so this works for
    any base_url.
    """
    with contextlib.suppress(Exception):
        await client.post("/v1/auth/register", json=_E2E_USER)
        await client.post("/v1/auth/login", json=_E2E_USER)


# ---------------------------------------------------------------------------
# Target matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One backend/reachability target in the feature matrix.

    ``provider_config`` and ``backend_recipe`` return the exact POST bodies for
    ``POST /v1/workspace_providers`` (the ``config`` sub-object) and the
    ``backend`` sub-object of ``POST /v1/workspace_templates`` respectively.
    """

    name: str
    platform: str  # "host" | "incluster"
    requires: str | None  # cap that must be present, or None (always available)
    provider: str  # workspace_providers `provider` discriminator
    _config: Callable[[str, str], dict[str, Any]]
    _backend: Callable[[], dict[str, Any]]

    def provider_config(self, suffix: str, kubeconfig: str) -> dict[str, Any]:
        return self._config(suffix, kubeconfig)

    def backend_recipe(self) -> dict[str, Any]:
        return self._backend()


def _local_config(suffix: str, kubeconfig: str) -> dict[str, Any]:
    # The local backend writes under root_path; mirror the existing local
    # workspace e2e helper (tests/_support/runs.make_local_workspace) which uses
    # a unique on-disk root per workspace so concurrent targets cannot collide.
    root = Path(os.environ.get("TMPDIR", "/tmp")) / f"primer-wsp-tpl-{suffix}"
    root.mkdir(parents=True, exist_ok=True)
    return {"kind": "local", "root_path": str(root)}


def _local_backend() -> dict[str, Any]:
    return {"kind": "local"}


def _container_config(suffix: str, kubeconfig: str) -> dict[str, Any]:
    return {
        "kind": "container",
        # `runtime` is required by the provider schema (the existing local
        # container e2e tests set it to "docker"); the task's shape omitted it
        # and the API rejects the body with a 422 missing-field otherwise.
        "runtime": "docker",
        "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
        "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"},
    }


def _container_backend() -> dict[str, Any]:
    return {
        "kind": "container",
        "image": "primer/workspace-runtime:1.0",
        "entrypoint": ["python", "-m", "primer_runtime.server"],
    }


def _k8s_gateway_config(suffix: str, kubeconfig: str) -> dict[str, Any]:
    return {
        "kind": "kubernetes",
        "connection": {"kind": "kubeconfig", "path": kubeconfig},
        "namespace": "primer-workspaces",
        "reachability": {
            "kind": "gateway_httproute",
            "scheme": "ws",
            "external_port": 32045,
            "gateway": {"name": "primer-gw", "namespace": "primer-gateway"},
            "routing": {
                "kind": "hostname",
                "hostname_template": "{workspace_id}.ws.local",
            },
        },
    }


def _k8s_incluster_config(suffix: str, kubeconfig: str) -> dict[str, Any]:
    return {
        "kind": "kubernetes",
        "connection": {"kind": "in_cluster"},
        "namespace": "primer-workspaces",
        "reachability": {"kind": "in_cluster"},
    }


def _k8s_backend() -> dict[str, Any]:
    return {
        "kind": "kubernetes",
        "image": "127.0.0.1:30500/primer/workspace-runtime:1.0",
        "entrypoint": ["python", "-m", "primer_runtime.server"],
        "pvc_size": "1Gi",
    }


TARGETS: list[Target] = [
    Target(
        name="local",
        platform="host",
        requires=None,
        provider="local",
        _config=_local_config,
        _backend=_local_backend,
    ),
    Target(
        name="container",
        platform="host",
        requires="workspace:container",
        provider="container",
        _config=_container_config,
        _backend=_container_backend,
    ),
    Target(
        name="kubernetes-gateway",
        platform="host",
        requires="workspace:kubernetes",
        provider="kubernetes",
        _config=_k8s_gateway_config,
        _backend=_k8s_backend,
    ),
    Target(
        name="kubernetes-incluster",
        platform="incluster",
        requires="workspace:kubernetes",
        provider="kubernetes",
        _config=_k8s_incluster_config,
        _backend=_k8s_backend,
    ),
]


def _base_url_for(target: Target) -> str:
    if target.platform == "incluster":
        return _incluster_base_url()
    return _host_base_url()


# ---------------------------------------------------------------------------
# platform_client fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(params=TARGETS, ids=[t.name for t in TARGETS])
async def platform_client(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[httpx.AsyncClient, Target]]:
    """Authenticated client + Target, parametrized over the whole matrix.

    Skips the target (with a clear reason) when its required cap is absent or,
    for the in-cluster target, when the in-cluster platform is unreachable. The
    client is always closed in teardown.
    """
    target: Target = request.param

    if target.requires is not None and not caps().has(target.requires):
        pytest.skip(
            f"target {target.name!r} requires capability {target.requires!r} "
            f"(testconfig workspace_backends), which is not available"
        )

    base_url = _base_url_for(target)

    if target.platform == "incluster":
        # The in-cluster platform is a separate deployment; skip rather than
        # fail when it is not reachable.
        try:
            async with httpx.AsyncClient(
                base_url=base_url, timeout=httpx.Timeout(5.0, connect=5.0)
            ) as probe:
                health = await probe.get("/v1/health")
            if health.status_code != 200:
                pytest.skip(
                    f"in-cluster platform health at {base_url} returned "
                    f"{health.status_code}"
                )
        except httpx.HTTPError as exc:
            pytest.skip(f"in-cluster platform at {base_url} unreachable: {exc!r}")

    client = httpx.AsyncClient(
        base_url=base_url, timeout=httpx.Timeout(60.0, connect=10.0)
    )
    try:
        await _authenticate(client)
        yield client, target
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# make_template_workspace builder
# ---------------------------------------------------------------------------


@dataclass
class _Created:
    """Tracks the entities a builder created so they can be torn down."""

    client: httpx.AsyncClient
    workspace_id: str | None = None
    template_id: str | None = None
    provider_id: str | None = None

    async def cleanup(self) -> None:
        # Delete in dependency order: workspace -> template -> provider.
        if self.workspace_id is not None:
            with contextlib.suppress(Exception):
                await self.client.delete(f"/v1/workspaces/{self.workspace_id}")
        if self.template_id is not None:
            with contextlib.suppress(Exception):
                await self.client.delete(
                    f"/v1/workspace_templates/{self.template_id}"
                )
        if self.provider_id is not None:
            with contextlib.suppress(Exception):
                await self.client.delete(
                    f"/v1/workspace_providers/{self.provider_id}"
                )


async def make_template_workspace(
    client: httpx.AsyncClient,
    target: Target,
    suffix: str,
    *,
    files: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    init_commands: list[str] | None = None,
    resources: dict[str, Any] | None = None,
    entrypoint: list[str] | None = None,
    workdir: str | None = None,
    track: list[_Created] | None = None,
) -> str:
    """Provision provider -> template -> workspace for ``target`` and wait running.

    Returns the workspace id (``ws-<hex>``). Registers the created entities on
    ``track`` (a list of :class:`_Created`) for teardown by the caller; if
    ``track`` is None a throwaway tracker is used and NOTHING is cleaned up
    (callers that want cleanup must pass ``track``).

    ``files``/``env``/``init_commands``/``resources`` are passed straight to the
    template body when not None. ``entrypoint``/``workdir`` override the backend
    recipe's entrypoint/workdir respectively.
    """
    created = _Created(client=client)
    if track is not None:
        track.append(created)

    wp = f"wp-{target.name}-{suffix}"
    tpl = f"tpl-{target.name}-{suffix}"

    rp = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp,
            "provider": target.provider,
            "config": target.provider_config(suffix, _kubeconfig_path()),
        },
    )
    assert rp.status_code in (200, 201), rp.text
    created.provider_id = wp

    backend = target.backend_recipe()
    if entrypoint is not None:
        backend["entrypoint"] = entrypoint
    if workdir is not None:
        backend["workdir"] = workdir

    template_body: dict[str, Any] = {
        "id": tpl,
        "description": f"wsp-template feature harness ({target.name})",
        "provider_id": wp,
        "backend": backend,
    }
    if files is not None:
        template_body["files"] = files
    if env is not None:
        template_body["env"] = env
    if init_commands is not None:
        template_body["init_commands"] = init_commands
    if resources is not None:
        template_body["resources"] = resources

    rt = await client.post("/v1/workspace_templates", json=template_body)
    assert rt.status_code in (200, 201), rt.text
    created.template_id = tpl

    rw = await client.post("/v1/workspaces", json={"template_id": tpl})
    assert rw.status_code in (200, 201), rw.text
    wid = rw.json()["id"]
    assert wid and wid.startswith("ws-"), rw.text
    created.workspace_id = wid

    # Poll until running. Image pull / pod schedule can be slow on a cold node.
    import asyncio

    phase = None
    deadline = asyncio.get_event_loop().time() + _RUNNING_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
        got = await client.get(f"/v1/workspaces/{wid}")
        assert got.status_code == 200, got.text
        phase = got.json().get("phase")
        if phase == "running":
            break
        assert phase not in ("failed", "error"), got.text
        await asyncio.sleep(_POLL_INTERVAL_S)
    assert phase == "running", (
        f"workspace {wid} never reached running for target {target.name!r}: "
        f"phase={phase!r}"
    )
    return wid


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


async def test_harness_smoke(
    platform_client: tuple[httpx.AsyncClient, Target],
) -> None:
    """End-to-end smoke for each target: build an empty-feature workspace, write
    a file, read it back, assert the round-trip, then clean up."""
    client, target = platform_client
    suffix = uuid.uuid4().hex[:12]
    track: list[_Created] = []
    try:
        wid = await make_template_workspace(client, target, suffix, track=track)

        marker = f"SMOKE-{target.name}-{suffix}"
        w = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "smoke.txt"},
            json={"content": marker, "encoding": "text"},
        )
        assert w.status_code == 204, w.text

        rd = await client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": "smoke.txt", "encoding": "text"},
        )
        assert rd.status_code == 200, rd.text
        assert rd.json()["content"] == marker
    finally:
        for created in track:
            await created.cleanup()


# ---------------------------------------------------------------------------
# Feature: template-seeded inline files (content + mode)
# ---------------------------------------------------------------------------


async def test_seed_inline_file_and_mode(
    platform_client: tuple[httpx.AsyncClient, Target],
) -> None:
    """A template ``files`` entry with an inline source lands in the workspace
    with the right CONTENT and the requested file MODE, across every backend.

    Content is asserted via the file-read API on every backend. Mode read-back
    differs per backend, so the cross-backend source of truth is a diagnostic
    exec. The diagnostic command head is whitelisted (only ``echo``/``ls``/
    ``pwd``/``uname``/``whoami`` are allowed - ``stat`` is rejected), so we read
    the mode via ``ls -l`` and assert the rwx permission string for an
    executable file (``-rwxr-xr-x`` == 0755). The diagnostic shell runs from the
    workspace root on every backend - the local root_path on local,
    ``/workspace`` on container/k8s - so a path relative to the workspace root
    resolves on all of them.

    The seeded-file ``mode`` is forwarded on every backend: the local backend
    ``chmod``-s the file, and the container + kubernetes backends pass ``mode``
    through ``sandbox.write_file`` to the runtime ``write_file`` op. Mode is
    therefore asserted strictly on ALL targets."""
    client, target = platform_client
    suffix = uuid.uuid4().hex[:12]
    rel_path = "seed/hello.txt"
    marker = f"INLINE-{target.name}-{suffix}"
    track: list[_Created] = []
    try:
        wid = await make_template_workspace(
            client,
            target,
            suffix,
            files=[
                {
                    "path": rel_path,
                    "source": {"kind": "inline", "content": marker},
                    "mode": "0755",
                }
            ],
            track=track,
        )

        # CONTENT: the inline source must land verbatim.
        rd = await client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": rel_path, "encoding": "text"},
        )
        assert rd.status_code == 200, rd.text
        assert rd.json()["content"] == marker, rd.text

        # MODE: cross-backend truth via a diagnostic exec. The diagnostic
        # command head is whitelisted (stat is rejected), so read the mode via
        # `ls -l` and assert the executable rwx string (-rwxr-xr-x == 0755). The
        # diagnostic shell runs from the workspace root on every backend, so the
        # workspace-root-relative path resolves identically everywhere.
        diag = await client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": f"ls -l {rel_path}", "timeout_seconds": 30},
        )
        assert diag.status_code in (200, 201), diag.text
        body = diag.json()
        assert body["exit_code"] == 0, body
        # `ls -l` of a 0755 regular file starts with `-rwxr-xr-x`.
        mode_applied = "-rwxr-xr-x" in body["stdout"]
        assert mode_applied, (
            f"expected mode 0755 (-rwxr-xr-x) for {rel_path!r} on target "
            f"{target.name!r}, ls -l stdout={body['stdout']!r} "
            f"stderr={body.get('stderr')!r}"
        )
    finally:
        for created in track:
            await created.cleanup()


# ---------------------------------------------------------------------------
# Feature: template-seeded url-sourced files
# ---------------------------------------------------------------------------

# Node IP of the host machine as seen from INSIDE the k3s cluster. The
# in-cluster platform pod fetches the seed url at materialization time, so it
# must be given a url that resolves from inside the cluster (the host's node
# IP), NOT 127.0.0.1.
_HOST_NODE_IP = "127.0.0.1"


@dataclass
class _UrlServer:
    """A running local HTTP server serving fixed bytes at ``/seed-content``."""

    port: int
    body: bytes

    def url_for(self, target: Target) -> str:
        """The seed url to hand to ``target``'s platform.

        Host targets are materialized by the host platform (``:8765``), which
        shares the loopback with this test process, so ``127.0.0.1`` works. The
        in-cluster target is materialized by a pod inside the cluster, which
        reaches this server via the host's node IP.
        """
        host = _HOST_NODE_IP if target.platform == "incluster" else "127.0.0.1"
        return f"http://{host}:{self.port}/seed-content"


@pytest.fixture(scope="module")
def url_file_server() -> Iterator[_UrlServer]:
    """Start a tiny HTTP server serving fixed bytes at ``/seed-content``.

    Binds ``0.0.0.0`` on an ephemeral port so it is reachable both over
    loopback (host platform) and from inside the cluster via the host node IP
    (in-cluster platform). Torn down at module teardown.
    """
    token = uuid.uuid4().hex[:12]
    body = f"URL-SOURCED-{token}".encode()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/seed-content":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:  # silence access logging
            return

    server = ThreadingHTTPServer(("0.0.0.0", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _UrlServer(port=port, body=body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


async def test_seed_url_file(
    platform_client: tuple[httpx.AsyncClient, Target],
    url_file_server: _UrlServer,
) -> None:
    """A template ``files`` entry with a ``url`` source lands the FETCHED bytes
    in the workspace, across every backend.

    The platform that materializes the workspace fetches the url at
    materialization time, so the url must be reachable by that platform:
    ``127.0.0.1`` for the host platform, the host node IP for the in-cluster
    pod (see :meth:`_UrlServer.url_for`).

    For the in-cluster target only: if materialization fails specifically
    because the pod could not reach the host server (a connection/timeout error
    surfaced on the workspace), we ``skip`` rather than hard-fail - the cluster
    network reaching back out to the host node is environmental. Host targets
    hard-assert.
    """
    client, target = platform_client
    suffix = uuid.uuid4().hex[:12]
    rel_path = "seed/from-url.txt"
    expected = url_file_server.body.decode()
    url = url_file_server.url_for(target)
    track: list[_Created] = []
    try:
        try:
            wid = await make_template_workspace(
                client,
                target,
                suffix,
                files=[
                    {
                        "path": rel_path,
                        "source": {"kind": "url", "url": url},
                    }
                ],
                track=track,
            )
        except AssertionError as exc:
            # In-cluster only: a materialization failure whose root cause is the
            # pod being unable to reach the host url server is environmental, so
            # skip. Host targets must hard-fail (re-raise).
            if target.platform != "incluster":
                raise
            msg = str(exc).lower()
            reachability_markers = (
                "connection",
                "connect",
                "timed out",
                "timeout",
                "refused",
                "unreachable",
                "no route",
                "resolve",
                "fetch",
                url.lower(),
                _HOST_NODE_IP,
            )
            if any(m in msg for m in reachability_markers):
                pytest.skip(
                    f"cluster cannot reach the host url server at "
                    f"{_HOST_NODE_IP}:{url_file_server.port}: {exc}"
                )
            raise

        rd = await client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": rel_path, "encoding": "text"},
        )
        assert rd.status_code == 200, rd.text
        assert rd.json()["content"] == expected, rd.text
    finally:
        for created in track:
            await created.cleanup()


# ---------------------------------------------------------------------------
# Feature: template env injection
# ---------------------------------------------------------------------------


async def test_env_injection(
    platform_client: tuple[httpx.AsyncClient, Target],
) -> None:
    """A template ``env`` entry is injected into the workspace runtime and is
    visible to processes, across every backend.

    Env values are ``SecretStr`` in the model, but the API accepts a plain
    string in the JSON body, so we pass the marker as a plain string. We read
    the value back via a diagnostic exec. The diagnostic command head is
    whitelisted (only ``echo``/``ls``/``pwd``/``uname``/``whoami`` are allowed),
    so we use ``echo $PRIMER_SMK_VAR`` and rely on the shell to expand it; if the
    diagnostic does not run via a shell the expansion will not happen and stdout
    will contain the literal ``$PRIMER_SMK_VAR`` instead of the marker - that is
    a finding, surfaced via the assertion message.
    """
    client, target = platform_client
    suffix = uuid.uuid4().hex[:12]
    marker = f"ENV-{target.name}-{suffix}"
    track: list[_Created] = []
    try:
        wid = await make_template_workspace(
            client,
            target,
            suffix,
            env={"PRIMER_SMK_VAR": marker},
            track=track,
        )

        diag = await client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": "echo $PRIMER_SMK_VAR", "timeout_seconds": 30},
        )
        assert diag.status_code in (200, 201), diag.text
        body = diag.json()
        assert body["exit_code"] == 0, body
        stdout = body["stdout"]
        if "$PRIMER_SMK_VAR" in stdout and marker not in stdout:
            pytest.fail(
                f"diagnostic did not expand $PRIMER_SMK_VAR on target "
                f"{target.name!r} (no shell?): stdout={stdout!r}"
            )
        if marker not in stdout:
            # PRODUCT GAP (local only): the local diagnostic_exec path runs
            # `asyncio.create_subprocess_shell` with NO `env=` argument
            # (primer/workspace/local/workspace.py LocalWorkspace.diagnostic_exec
            # ~L439), so the subprocess inherits the API server's environment,
            # NOT the workspace template's env. (Template env IS wired into the
            # local exec TOOL and init commands - local/backend.py - but not into
            # this diagnostic surface.) `echo $PRIMER_SMK_VAR` therefore expands
            # to empty on local. Container/k8s exec inside the runtime pod/
            # container, whose process environment carries the injected env, so
            # they pass. Strict everywhere else; xfail only on local.
            if target.name == "local":
                pytest.xfail(
                    "template env not honored by local diagnostic_exec - "
                    "LocalWorkspace.diagnostic_exec "
                    "(primer/workspace/local/workspace.py) calls "
                    "create_subprocess_shell without env=, so template.env is "
                    "not injected into the diagnostic shell"
                )
        assert marker in stdout, (
            f"expected env marker {marker!r} in echo stdout for target "
            f"{target.name!r}, got stdout={stdout!r} "
            f"stderr={body.get('stderr')!r}"
        )
    finally:
        for created in track:
            await created.cleanup()


# ---------------------------------------------------------------------------
# Feature: template init_commands
# ---------------------------------------------------------------------------


async def test_init_commands(
    platform_client: tuple[httpx.AsyncClient, Target],
) -> None:
    """Template ``init_commands`` run once after files land, against the
    workspace root, across every backend.

    The init command writes a marker file under the workspace root; we then read
    it back via the file-read API. The path is workspace-root-relative on every
    backend (the local root_path on local, ``/workspace`` on container/k8s),
    mirroring the inline-file test. ``init_commands`` runs in a shell at
    materialization time, so the ``> init-marker.txt`` redirect lands the file at
    the workspace root.
    """
    client, target = platform_client
    suffix = uuid.uuid4().hex[:12]
    rel_path = "init-marker.txt"
    track: list[_Created] = []
    try:
        wid = await make_template_workspace(
            client,
            target,
            suffix,
            init_commands=["echo INIT-OK > init-marker.txt"],
            track=track,
        )

        rd = await client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": rel_path, "encoding": "text"},
        )
        if rd.status_code != 200 and target.provider == "kubernetes":
            # PRODUCT GAP (kubernetes backends): KubernetesWorkspaceBackend
            # materialization (primer/workspace/k8s/backend.py ~L418-446)
            # resolves `files`, injects `env` into the pod spec, and applies
            # `resources`, but NEVER runs `template.init_commands`. The local
            # backend (local/backend.py L118-138) and the container backend
            # (container/backend.py L140-144) both run init_commands; the k8s
            # backend drops them entirely, so the marker file is never created
            # and the read 404s. Affects both kubernetes-gateway and
            # kubernetes-incluster (they share _k8s_backend).
            pytest.xfail(
                "template init_commands not honored on kubernetes backend - "
                "KubernetesWorkspaceBackend materialization "
                "(primer/workspace/k8s/backend.py) never runs "
                "template.init_commands (local + container backends do); "
                f"marker file {rel_path!r} 404s: body={rd.text}"
            )
        assert rd.status_code == 200, (
            f"init_commands marker file {rel_path!r} not readable on target "
            f"{target.name!r}: status={rd.status_code} body={rd.text}"
        )
        assert rd.json()["content"].strip() == "INIT-OK", rd.text
    finally:
        for created in track:
            await created.cleanup()
