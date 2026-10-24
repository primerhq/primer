"""Per-workspace size and env overrides (platform revamp 2026-10, plan 08).

``POST /v1/workspaces`` takes ``overrides.env`` and ``overrides.resources`` (``cpu_cores``, ``memory_bytes``, ``disk_bytes``).
On Kubernetes both land in the workspace's pod: the override env in the container's ``env`` (it used to reach only the init
commands), the size in the container's requests and limits and in the PVC request. A size the workspace's provider cannot
honour is a 422 naming ``overrides.resources.<field>`` (Local takes none, Docker has no disk quota).

Lead-scheduled. ``test_kubernetes_override_env_and_size_reach_the_pod`` needs a Kubernetes cluster the platform runs inside
(``workspace:kubernetes``), like ``test_smk_workspaces.py::test_kubernetes_backend``, whose topology notes apply (point
``PRIMER_E2E_BASE_URL`` at the in-cluster ``primer-api`` Service; ``PRIMER_K8S_RUNTIME_IMAGE`` or ``PRIMER_K8S_REGISTRY`` names a
cluster-pullable runtime image; ``PRIMER_K8S_NAMESPACE`` defaults to ``primer-workspaces``).
``test_a_docker_workspace_gets_its_cpu_and_memory`` needs a Docker daemon the server reaches (``workspace:container``) and the
``primer/workspace-runtime:1.1`` image, and reads the limits back with ``docker inspect``. The refusal tests need only the
server. Never run any of them alongside another e2e run.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import pytest

from tests._support.testconfig import requires

pytestmark = pytest.mark.asyncio


def _k8s_runtime_image(tag: str) -> str:
    """Cluster-pullable workspace-runtime image for the k8s lane (the same rule as ``test_smk_workspaces``)."""
    explicit = os.environ.get("PRIMER_K8S_RUNTIME_IMAGE")
    if explicit:
        return explicit
    registry = os.environ.get("PRIMER_K8S_REGISTRY", "localhost:30500")
    return f"{registry}/primer/workspace-runtime:{tag}"


def _kubectl_json(kind: str, name: str, namespace: str) -> dict | None:
    """``kubectl get <kind> <name> -o json`` parsed, or None when kubectl is missing or the object is not there."""
    if shutil.which("kubectl") is None:
        return None
    try:
        out = subprocess.run(
            ["kubectl", "get", kind, name, "-n", namespace, "-o", "json"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None


_QUANTITY_SUFFIXES = {"m": 1e-3, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


def _quantity(text: str) -> float:
    """A Kubernetes quantity as a number, so ``500m`` equals ``0.5`` and ``2Gi`` equals ``2147483648`` whichever form the API
    server reports."""
    for suffix, factor in _QUANTITY_SUFFIXES.items():
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text)


@requires("workspace:kubernetes")
async def test_kubernetes_override_env_and_size_reach_the_pod(authed_client, unique_suffix):
    namespace = os.environ.get("PRIMER_K8S_NAMESPACE", "primer-workspaces")
    wp = f"wpo-{unique_suffix}"
    tpl = f"tplo-{unique_suffix}"
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
            "description": "plan 08 size and env overrides",
            "provider_id": wp,
            "backend": {
                "kind": "kubernetes",
                "image": _k8s_runtime_image("1.0"),
                # Without an entrypoint the backend runs `sleep infinity` and the runtime never starts.
                "entrypoint": ["python", "-m", "primer_runtime.server"],
                "pvc_size": "1Gi",
            },
        },
    )
    assert rt.status_code in (200, 201), rt.text

    try:
        rw = await authed_client.post(
            "/v1/workspaces",
            json={
                "template_id": tpl,
                "overrides": {
                    "env": {"PLAN08_ENV": "from-override"},
                    "resources": {"cpu_cores": 0.5, "memory_bytes": 268435456, "disk_bytes": 2147483648},
                },
            },
        )
        assert rw.status_code in (200, 201), rw.text
        body = rw.json()
        wid = body["id"]
        obj_name = body["runtime_meta"]["k8s_object_name"]
        assert body["overrides"]["resources"] == {"cpu_cores": 0.5, "memory_bytes": 268435456, "disk_bytes": 2147483648}

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

        # The override env is in the pod's own environment, not only in the init commands' (the diagnostic runs a
        # program in the pod's container and inherits that environment).
        diag = await authed_client.post(
            f"/v1/workspaces/{wid}/diagnostic", json={"command": "printenv PLAN08_ENV", "timeout_seconds": 30},
        )
        assert diag.status_code in (200, 201), diag.text
        result = diag.json()
        assert result["exit_code"] == 0, result
        assert result["stdout"].strip() == "from-override", result

        # The size is on the container and on the PVC (checked when kubectl is on the path; the API server may
        # report a quantity in another form than it was sent, hence the numeric comparison).
        pod = _kubectl_json("pod", f"{obj_name}-0", namespace)
        if pod is not None:
            resources = pod["spec"]["containers"][0]["resources"]
            for section in ("limits", "requests"):
                assert _quantity(resources[section]["cpu"]) == pytest.approx(0.5), resources
                assert _quantity(resources[section]["memory"]) == 268435456, resources
        pvc = _kubectl_json("pvc", f"ws-{obj_name}-0", namespace)
        if pvc is not None:
            assert _quantity(pvc["spec"]["resources"]["requests"]["storage"]) == 2147483648, pvc
    finally:
        if wid is not None:
            d = await authed_client.delete(f"/v1/workspaces/{wid}")
            assert d.status_code in (204, 404), d.text
        await authed_client.delete(f"/v1/workspace_templates/{tpl}")
        await authed_client.delete(f"/v1/workspace_providers/{wp}")


# ---- a size the provider cannot honour is refused (plan 08 task 4; master Review Focus 5) ----------------------------------


async def _provider_and_template(authed_client, *, wp: str, tpl: str, provider: dict, backend: dict) -> None:
    rp = await authed_client.post("/v1/workspace_providers", json={"id": wp, **provider})
    assert rp.status_code in (200, 201), rp.text
    rt = await authed_client.post(
        "/v1/workspace_templates",
        json={"id": tpl, "description": "plan 08 size refusal", "provider_id": wp, "backend": backend},
    )
    assert rt.status_code in (200, 201), rt.text


async def _drop(authed_client, *, wp: str, tpl: str) -> None:
    await authed_client.delete(f"/v1/workspace_templates/{tpl}")
    await authed_client.delete(f"/v1/workspace_providers/{wp}")


async def test_a_local_workspace_cannot_be_sized(authed_client, unique_suffix, tmp_path):
    """A Local workspace runs on the server itself: any size field is a 422 naming it, and nothing is created."""
    wp, tpl = f"wpl-{unique_suffix}", f"tpll-{unique_suffix}"
    await _provider_and_template(
        authed_client, wp=wp, tpl=tpl,
        provider={"provider": "local", "config": {"kind": "local", "root_path": str(tmp_path)}},
        backend={"kind": "local"},
    )
    try:
        r = await authed_client.post(
            "/v1/workspaces", json={"template_id": tpl, "overrides": {"resources": {"cpu_cores": 1}}},
        )
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["type"] == "/errors/validation-error", body
        assert body["extensions"]["errors"][0]["loc"] == ["body", "overrides", "resources", "cpu_cores"], body
        assert body["extensions"]["errors"][0]["type"] == "unsupported_by_provider", body
        listing = await authed_client.get("/v1/workspaces")
        assert not [w for w in listing.json()["items"] if w.get("template_id") == tpl], listing.text
    finally:
        await _drop(authed_client, wp=wp, tpl=tpl)


async def test_a_docker_disk_override_is_refused(authed_client, unique_suffix):
    """Docker has no disk quota. Only the provider and template ROWS exist (no daemon is needed: the check runs before the
    backend is asked to build anything), and the refusal names ``disk_bytes``."""
    wp, tpl = f"wpd-{unique_suffix}", f"tpld-{unique_suffix}"
    await _provider_and_template(
        authed_client, wp=wp, tpl=tpl,
        provider={
            "provider": "container",
            "config": {
                "kind": "container", "runtime": "docker",
                "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
                "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"},
            },
        },
        backend={"kind": "container", "image": "primer/workspace-runtime:1.1"},
    )
    try:
        r = await authed_client.post(
            "/v1/workspaces", json={"template_id": tpl, "overrides": {"resources": {"disk_bytes": 10737418240}}},
        )
        assert r.status_code == 422, r.text
        errors = r.json()["extensions"]["errors"]
        assert [e["loc"] for e in errors] == [["body", "overrides", "resources", "disk_bytes"]], errors
        assert errors[0]["msg"] == "Docker cannot limit a workspace's disk. Remove this override.", errors
    finally:
        await _drop(authed_client, wp=wp, tpl=tpl)


# ---- sane maxima and bad env names are a 422, never a 500 after a rollback (lead ruling 17 of plan 08) ---------------------


async def test_an_absurd_size_override_is_a_422_not_a_500(authed_client, unique_suffix):
    """1e308 cores used to reach the cluster and come back as a generic 500 after a rolled-back create. Only the provider and
    template ROWS exist (no cluster is needed: the model refuses the number before any backend runs)."""
    wp, tpl = f"wpk-{unique_suffix}", f"tplk-{unique_suffix}"
    await _provider_and_template(
        authed_client, wp=wp, tpl=tpl,
        provider={
            "provider": "kubernetes",
            "config": {
                "kind": "kubernetes", "connection": {"kind": "in_cluster"}, "namespace": "primer-workspaces",
                "reachability": {"kind": "in_cluster"},
            },
        },
        backend={"kind": "kubernetes", "image": "primer/workspace-runtime:1.1"},
    )
    try:
        for field, absurd in (("cpu_cores", 1e308), ("memory_bytes", 10**30), ("disk_bytes", 10**30)):
            r = await authed_client.post(
                "/v1/workspaces", json={"template_id": tpl, "overrides": {"resources": {field: absurd}}},
            )
            assert r.status_code == 422, (field, r.text)
            errors = r.json()["extensions"]["errors"]
            assert [e["loc"] for e in errors] == [["body", "overrides", "resources", field]], (field, errors)
    finally:
        await _drop(authed_client, wp=wp, tpl=tpl)


async def test_a_bad_environment_variable_name_is_a_422_that_names_it(authed_client, unique_suffix, tmp_path):
    """A name the cluster would refuse, and the two names the platform sets itself, are refused before anything is built; the
    value (a secret, perhaps) is never in the answer."""
    wp, tpl = f"wpe-{unique_suffix}", f"tple-{unique_suffix}"
    await _provider_and_template(
        authed_client, wp=wp, tpl=tpl,
        provider={"provider": "local", "config": {"kind": "local", "root_path": str(tmp_path)}},
        backend={"kind": "local"},
    )
    try:
        for name, kind in (("1BAD", "invalid_env_name"), ("a b", "invalid_env_name"), ("RUNTIME_TOKEN", "reserved_env_name")):
            r = await authed_client.post(
                "/v1/workspaces", json={"template_id": tpl, "overrides": {"env": {name: "plan08-secret-value"}}},
            )
            assert r.status_code == 422, (name, r.text)
            error = r.json()["extensions"]["errors"][0]
            assert error["loc"] == ["body", "overrides", "env", name] and error["type"] == kind, (name, error)
            assert "plan08-secret-value" not in r.text, name
        listing = await authed_client.get("/v1/workspaces")
        assert not [w for w in listing.json()["items"] if w.get("template_id") == tpl], listing.text
    finally:
        await _drop(authed_client, wp=wp, tpl=tpl)


# ---- Docker applies the per-workspace CPU and memory (plan 08 task 5) -----------------------------------------------------


def _docker_inspect(name: str) -> dict | None:
    """``docker inspect <name>`` parsed (the first object), or None when docker is not on the path or the container is not there."""
    if shutil.which("docker") is None:
        return None
    try:
        out = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    parsed = json.loads(out.stdout)
    return parsed[0] if parsed else None


@requires("workspace:container")
async def test_a_docker_workspace_gets_its_cpu_and_memory(authed_client, unique_suffix):
    """The override replaces the template's CPU and memory on the container (``NanoCpus`` and ``Memory``), and the template's
    image is the image that runs. The template says 1 CPU and 512 MiB; the workspace asks for half a CPU and 256 MiB."""
    wp, tpl = f"wpdk-{unique_suffix}", f"tpldk-{unique_suffix}"
    wid: str | None = None
    await _provider_and_template(
        authed_client, wp=wp, tpl=tpl,
        provider={
            "provider": "container",
            "config": {
                "kind": "container", "runtime": "docker",
                "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
                "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"},
            },
        },
        backend={"kind": "container", "image": "primer/workspace-runtime:1.1", "cpu_cores": 1, "memory_bytes": 536870912},
    )
    try:
        rw = await authed_client.post(
            "/v1/workspaces",
            json={"template_id": tpl, "overrides": {"resources": {"cpu_cores": 0.5, "memory_bytes": 268435456}}},
        )
        assert rw.status_code in (200, 201), rw.text
        wid = rw.json()["id"]
        info = _docker_inspect(f"workspace-{wid}")
        assert info is not None, "workspace:container is enabled, but docker inspect could not read the workspace container"
        assert info["HostConfig"]["NanoCpus"] == 500000000, info["HostConfig"]
        assert info["HostConfig"]["Memory"] == 268435456, info["HostConfig"]
        assert info["Config"]["Image"] == "primer/workspace-runtime:1.1", info["Config"]["Image"]
        assert info["Config"]["Labels"]["primer.workspace.template.image"] == "primer/workspace-runtime:1.1"
    finally:
        if wid is not None:
            d = await authed_client.delete(f"/v1/workspaces/{wid}")
            assert d.status_code in (204, 404), d.text
        await _drop(authed_client, wp=wp, tpl=tpl)


# ---- non-admins pick a provider by name (plan 08 task 7) -----------------------------------------------------------------


async def test_provider_choices_list_the_local_provider(authed_client):
    """``GET /v1/workspace_providers/_choices`` answers a signed-in user with every provider's id, kind and label and nothing of its
    configuration. The reserved ``local`` provider is seeded on boot, so it is always there; a Local provider has no image to pre-fill."""
    r = await authed_client.get("/v1/workspace_providers/_choices")

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i for i in items if i["id"] == "local"] == [{"id": "local", "kind": "local", "label": "Local", "default_image": None}], items
    assert all(set(i) == {"id", "kind", "label", "default_image"} for i in items), items
    assert [i["id"] for i in items] == sorted(i["id"] for i in items), "sorted by id"
