"""Per-workspace size and env overrides against a real cluster (platform revamp 2026-10, plan 08).

``POST /v1/workspaces`` takes ``overrides.env`` and ``overrides.resources`` (``cpu_cores``, ``memory_bytes``, ``disk_bytes``).
On Kubernetes both land in the workspace's pod: the override env in the container's ``env`` (it used to reach only the init
commands), the size in the container's requests and limits and in the PVC request.

Lead-scheduled: this needs a Kubernetes cluster the platform runs inside (``workspace:kubernetes``), like
``test_smk_workspaces.py::test_kubernetes_backend``, whose topology notes apply (point ``PRIMER_E2E_BASE_URL`` at the in-cluster
``primer-api`` Service; ``PRIMER_K8S_RUNTIME_IMAGE`` or ``PRIMER_K8S_REGISTRY`` names a cluster-pullable runtime image;
``PRIMER_K8S_NAMESPACE`` defaults to ``primer-workspaces``). Never run it alongside another e2e run.
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
