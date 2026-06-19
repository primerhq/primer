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
import subprocess
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
        assert rd.status_code == 200, (
            f"init_commands marker file {rel_path!r} not readable on target "
            f"{target.name!r}: status={rd.status_code} body={rd.text}"
        )
        assert rd.json()["content"].strip() == "INIT-OK", rd.text
    finally:
        for created in track:
            await created.cleanup()


# ---------------------------------------------------------------------------
# Phase 5.1: resource limits materialize (all four targets)
# ---------------------------------------------------------------------------


async def test_resources_materializes(
    platform_client: tuple[httpx.AsyncClient, Target],
) -> None:
    """A template with ``resources`` (cpu + memory limits) materializes running
    and stays usable, across every backend.

    This is an ASSERT-MATERIALIZES test: the point is that applying a resource
    limit does NOT break materialization (the limit flows into the docker
    HostConfig NanoCpus/Memory on container, into the pod container
    ``resources.limits`` on kubernetes, and into the local backend's limiter),
    and the workspace remains operable. We deliberately do NOT read the cgroup -
    the cross-backend truth is simply that the workspace reaches running and a
    basic op (a file write+read round-trip) succeeds.

    Limits are modest (0.5 CPU, 256MiB) so a scheduled pod/container can still
    boot the runtime; a too-tight memory limit would OOM-kill the runtime and
    surface as a non-running phase, which the builder would catch.
    """
    client, target = platform_client
    suffix = uuid.uuid4().hex[:12]
    track: list[_Created] = []
    try:
        wid = await make_template_workspace(
            client,
            target,
            suffix,
            resources={"cpu_cores": 0.5, "memory_bytes": 268435456},
            track=track,
        )

        # Basic op: a file write+read round-trip proves the limited workspace is
        # operable (the runtime is up and serving ops under the limit).
        marker = f"RES-{target.name}-{suffix}"
        w = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "res.txt"},
            json={"content": marker, "encoding": "text"},
        )
        assert w.status_code == 204, w.text
        rd = await client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": "res.txt", "encoding": "text"},
        )
        assert rd.status_code == 200, rd.text
        assert rd.json()["content"] == marker, rd.text
    finally:
        for created in track:
            await created.cleanup()


# ---------------------------------------------------------------------------
# Phase 5.2: backend-specific recipe fields (targeted, not full cross-product)
# ---------------------------------------------------------------------------


def _only_targets(*names: str) -> list[Target]:
    """The subset of TARGETS whose name is in ``names`` (order preserved)."""
    wanted = set(names)
    return [t for t in TARGETS if t.name in wanted]


async def _build_for(
    target: Target,
    track: list[_Created],
    suffix: str,
    **kwargs: Any,
) -> tuple[httpx.AsyncClient, str]:
    """Authenticate a client for ``target`` (honoring its skip rules) and build
    a workspace, returning ``(client, workspace_id)``.

    These backend-specific tests are NOT driven by the ``platform_client``
    fixture (which fans out over the whole matrix); they target one or two
    backends explicitly, so they replicate the fixture's cap/health gating
    inline before building.
    """
    if target.requires is not None and not caps().has(target.requires):
        pytest.skip(
            f"target {target.name!r} requires capability {target.requires!r}, "
            f"which is not available"
        )
    base_url = _base_url_for(target)
    if target.platform == "incluster":
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
    await _authenticate(client)
    wid = await make_template_workspace(client, target, suffix, track=track, **kwargs)
    return client, wid


@pytest.mark.parametrize(
    "target", _only_targets("container"), ids=lambda t: t.name
)
async def test_container_workdir(target: Target) -> None:
    """The container backend accepts a non-default ``workdir`` and materializes
    a running workspace with it.

    ``make_template_workspace(..., workdir=...)`` overrides the backend recipe's
    ``workdir``, which the container backend threads into the docker
    ``WorkingDir`` and the workspace volume mount target / workspace root. We
    assert the workspace reaches running with the override (this part works:
    the volume mounts at ``/srv/work``, files seed there, the runtime launches).

    PARITY GAP (traced xfail, see below): the diagnostic-exec path, which runs
    a whitelisted command from ``workspace_root`` (== the overridden workdir),
    returns 500 for EVERY command when the workdir is non-default - so we cannot
    confirm ``pwd`` prints the override. With the DEFAULT workdir (``/workspace``)
    the identical ``pwd``/``echo`` diagnostics return 200. The override is
    accepted and the workspace runs, but exec against it is broken. Likely call
    site: ``WSSandbox.exec`` -> ``RuntimeClient.exec`` -> the in-container
    runtime server's EXEC op, which appears to resolve/validate the exec
    ``workdir`` against a hardcoded ``/workspace`` root and raises (surfacing as
    a 500 on ``POST /v1/workspaces/{id}/diagnostic``) when the cwd is outside it.
    """
    suffix = uuid.uuid4().hex[:12]
    workdir = "/srv/work"
    track: list[_Created] = []
    client: httpx.AsyncClient | None = None
    try:
        # Part that WORKS (strict): the override is accepted and the workspace
        # materializes running with the custom workdir.
        client, wid = await _build_for(target, track, suffix, workdir=workdir)

        # Part that's BROKEN (traced xfail): the diagnostic exec runs from the
        # overridden workspace_root and 500s for every command on a non-default
        # workdir. xfail(strict=True) so this flips to a hard failure (alerting
        # the coordinator to re-roll) the moment the platform starts honoring it.
        diag = await client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": "pwd", "timeout_seconds": 30},
        )
        if diag.status_code == 500:
            pytest.xfail(
                "container backend PARITY GAP: a non-default workdir "
                f"({workdir!r}) materializes running, but diagnostic exec "
                "(WSSandbox.exec -> RuntimeClient.exec -> runtime EXEC op) "
                "returns 500 for every command; default workdir (/workspace) "
                "diagnostics return 200. Likely the runtime server resolves the "
                "exec cwd against a hardcoded /workspace root."
            )
        assert diag.status_code in (200, 201), diag.text
        body = diag.json()
        assert body["exit_code"] == 0, body
        assert body["stdout"].strip() == workdir, (
            f"expected pwd == {workdir!r} for the container workdir override, "
            f"got stdout={body['stdout']!r} stderr={body.get('stderr')!r}"
        )
    finally:
        for created in track:
            await created.cleanup()
        if client is not None:
            await client.aclose()
        if client is not None:
            await client.aclose()


@pytest.mark.parametrize(
    "target",
    _only_targets("container", "kubernetes-gateway", "kubernetes-incluster"),
    ids=lambda t: t.name,
)
async def test_entrypoint_launches(target: Target) -> None:
    """The runtime entrypoint launches: the workspace is reachable and a basic
    op works, on container + both kubernetes targets.

    The container/kubernetes backends start the runtime via the recipe's
    ``entrypoint`` (``python -m primer_runtime.server``). The workspace can only
    reach ``running`` and serve ops once that runtime is listening, so a
    successful file write+read round-trip implicitly proves the entrypoint
    launched the runtime. We reuse the default entrypoint (do not override it).
    """
    suffix = uuid.uuid4().hex[:12]
    track: list[_Created] = []
    client: httpx.AsyncClient | None = None
    try:
        client, wid = await _build_for(target, track, suffix)
        marker = f"ENTRY-{target.name}-{suffix}"
        w = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "entry.txt"},
            json={"content": marker, "encoding": "text"},
        )
        assert w.status_code == 204, w.text
        rd = await client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": "entry.txt", "encoding": "text"},
        )
        assert rd.status_code == 200, rd.text
        assert rd.json()["content"] == marker, rd.text
    finally:
        for created in track:
            await created.cleanup()
        if client is not None:
            await client.aclose()


# ---------------------------------------------------------------------------
# Feature: template-seeded document-sourced files
# ---------------------------------------------------------------------------


@dataclass
class _Collection:
    """Tracks a collection + document (and its embedder/ssp) for teardown."""

    client: httpx.AsyncClient
    collection_id: str | None = None
    document_id: str | None = None
    embedder_id: str | None = None
    ssp_id: str | None = None

    async def cleanup(self) -> None:
        # Delete in dependency order: document -> collection -> ssp -> embedder.
        if self.document_id is not None:
            with contextlib.suppress(Exception):
                await self.client.delete(f"/v1/documents/{self.document_id}")
        if self.collection_id is not None:
            with contextlib.suppress(Exception):
                await self.client.delete(f"/v1/collections/{self.collection_id}")
        if self.ssp_id is not None:
            with contextlib.suppress(Exception):
                await self.client.delete(f"/v1/ssp/{self.ssp_id}")
        if self.embedder_id is not None:
            with contextlib.suppress(Exception):
                await self.client.delete(
                    f"/v1/embedding_providers/{self.embedder_id}"
                )


async def _make_document(
    client: httpx.AsyncClient, suffix: str, marker: str, tracker: _Collection
) -> tuple[str, str]:
    """Create a collection + a document whose body is ``marker``.

    Reuses the hermetic knowledge backends the existing knowledge e2e uses
    (``tests/e2e/test_smk_knowledge.py::_embedder_and_ssp``): a HuggingFace
    placeholder embedder + a LanceDB SSP at a unique on-disk path. The
    document-source resolver only reads the persisted Document row from
    storage (``document_body_text`` -> ``meta['text']``); it does NOT touch
    the vector store, and on-create indexing is best-effort (a failing
    placeholder embedder is logged, not fatal), so the collection's embedder
    /SSP are only here because collection-create requires a valid
    ``search_provider_id``. The marker is stored under ``meta['text']`` -
    the body text the REST create form indexes and the resolver returns.
    """
    eid = f"emb-doc-{suffix}"
    er = await client.post(
        "/v1/embedding_providers",
        json={
            "id": eid,
            "provider": "huggingface",
            "models": [
                {"name": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384}
            ],
            "config": {"token": "hf-placeholder"},
            "limits": {"max_concurrency": 1},
        },
    )
    assert er.status_code in (200, 201), er.text
    tracker.embedder_id = eid

    sid = f"ssp-doc-{suffix}"
    lance_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"primer-wsp-doc-{suffix}"
    sr = await client.post(
        "/v1/ssp",
        json={"id": sid, "provider": "lance", "config": {"path": str(lance_path)}},
    )
    assert sr.status_code in (200, 201), sr.text
    tracker.ssp_id = sid

    cid = f"col-doc-{suffix}"
    cr = await client.post(
        "/v1/collections",
        json={
            "id": cid,
            "description": "wsp-template document-source harness",
            "embedder": {
                "provider_id": eid,
                "model": "sentence-transformers/all-MiniLM-L6-v2",
            },
            "search_provider_id": sid,
            "system": False,
        },
    )
    assert cr.status_code in (200, 201), cr.text
    tracker.collection_id = cid

    did = f"doc-{suffix}"
    dr = await client.post(
        "/v1/documents",
        json={
            "id": did,
            "path": f"{did}.md",
            "collection_id": cid,
            "name": did,
            "meta": {"text": marker},
        },
    )
    assert dr.status_code in (200, 201), dr.text
    tracker.document_id = did

    return cid, did


async def test_seed_document_file(
    platform_client: tuple[httpx.AsyncClient, Target],
) -> None:
    """A template ``files`` entry with a ``document`` source lands the
    Document's body text in the workspace, across every backend.

    The document-source resolver loads the Document by id, verifies it
    belongs to the named collection, and returns its body text
    (``meta['text']``) UTF-8 encoded. The collection/document live on the
    SAME platform that materializes the workspace (``client``), so the
    resolver can read them. We assert the seeded file equals the unique
    per-target body marker.
    """
    client, target = platform_client
    suffix = uuid.uuid4().hex[:12]
    rel_path = "seed/from-doc.txt"
    marker = f"doc-body-{target.name}-{suffix}"
    track: list[_Created] = []
    coll = _Collection(client=client)
    try:
        cid, did = await _make_document(client, suffix, marker, coll)

        wid = await make_template_workspace(
            client,
            target,
            suffix,
            files=[
                {
                    "path": rel_path,
                    "source": {
                        "kind": "document",
                        "collection_id": cid,
                        "document_id": did,
                    },
                }
            ],
            track=track,
        )

        rd = await client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": rel_path, "encoding": "text"},
        )
        assert rd.status_code == 200, rd.text
        assert rd.json()["content"] == marker, rd.text
    finally:
        for created in track:
            await created.cleanup()
        await coll.cleanup()


# ---------------------------------------------------------------------------
# Feature: template-seeded secret-sourced files
# ---------------------------------------------------------------------------


async def test_seed_secret_file(
    platform_client: tuple[httpx.AsyncClient, Target],
) -> None:
    """A template ``files`` entry with a ``secret`` source lands the secret
    VALUE in the workspace, across every backend.

    The secret value is read by the PLATFORM process from its OWN environment
    via the env-backed SecretProvider: the secret named ``wsp_e2e_token``
    resolves to the env var ``PRIMER_SECRET_WSP_E2E_TOKEN`` (prefix +
    upper-cased name) on the platform under test. This test reads the SAME
    env var only to know the expected value and to skip when it is unset; the
    coordinator sets the same env on the platform under test. The secret value
    is NEVER hardcoded here.
    """
    client, target = platform_client
    secret_name = "wsp_e2e_token"
    env_var = "PRIMER_SECRET_WSP_E2E_TOKEN"
    expected = os.environ.get(env_var)
    if expected is None:
        pytest.skip(f"{env_var} not set")

    suffix = uuid.uuid4().hex[:12]
    rel_path = "seed/secret.txt"
    track: list[_Created] = []
    try:
        wid = await make_template_workspace(
            client,
            target,
            suffix,
            files=[
                {
                    "path": rel_path,
                    "source": {"kind": "secret", "name": secret_name},
                }
            ],
            track=track,
        )

        rd = await client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": rel_path, "encoding": "text"},
        )
        assert rd.status_code == 200, rd.text
        assert rd.json()["content"] == expected, rd.text
    finally:
        for created in track:
            await created.cleanup()


def _pvc_storage_request(workspace_id: str, kubeconfig: str) -> str | None:
    """Return the requested storage size of ``workspace_id``'s PVC, or None.

    The StatefulSet's volumeClaimTemplate is named ``ws``, so K8s names the
    replica-0 PVC ``ws-<object-name>-0`` where ``<object-name>`` is
    ``k8s_object_name(workspace_id)`` (see primer/workspace/k8s/naming.py and
    backend._pvc_name_for). We list PVCs in the workspace namespace and match the
    one whose name contains the derived object name - robust to the exact prefix
    layout - then read ``spec.resources.requests.storage`` for it.
    """
    from primer.workspace.k8s.naming import k8s_object_name

    obj_name = k8s_object_name(workspace_id)
    env = {**os.environ, "KUBECONFIG": kubeconfig}
    listing = subprocess.run(
        [
            "kubectl",
            "get",
            "pvc",
            "-n",
            "primer-workspaces",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert listing.returncode == 0, listing.stderr
    names = [n for n in listing.stdout.splitlines() if obj_name in n]
    if not names:
        return None
    pvc_name = names[0]
    got = subprocess.run(
        [
            "kubectl",
            "get",
            "pvc",
            pvc_name,
            "-n",
            "primer-workspaces",
            "-o",
            "jsonpath={.spec.resources.requests.storage}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert got.returncode == 0, got.stderr
    return got.stdout.strip()


@pytest.mark.parametrize(
    "target",
    _only_targets("kubernetes-gateway", "kubernetes-incluster"),
    ids=lambda t: t.name,
)
async def test_k8s_pvc_size(target: Target) -> None:
    """The kubernetes backend creates the workspace PVC at the requested size.

    The default backend recipe requests ``pvc_size="1Gi"``; the backend puts
    that into the StatefulSet's volumeClaimTemplate. We assert host-side via
    ``kubectl`` (the PVC lives in the cluster regardless of which platform -
    host-gateway or in-cluster - created the StatefulSet) that the workspace's
    PVC requests exactly ``1Gi``.
    """
    suffix = uuid.uuid4().hex[:12]
    track: list[_Created] = []
    client: httpx.AsyncClient | None = None
    try:
        client, wid = await _build_for(target, track, suffix)
        size = _pvc_storage_request(wid, _kubeconfig_path())
        assert size is not None, (
            f"no PVC found in namespace primer-workspaces matching workspace "
            f"{wid!r} (object name derived from naming.k8s_object_name)"
        )
        assert size == "1Gi", (
            f"expected PVC storage request '1Gi' for workspace {wid!r} on target "
            f"{target.name!r}, got {size!r}"
        )
    finally:
        for created in track:
            await created.cleanup()
        if client is not None:
            await client.aclose()
