"""Gated integration test for KubernetesWorkspaceBackend.list() against a
real cluster.

Requires:
- ``kubernetes-asyncio`` installed (checked via importorskip).
- A reachable cluster via the default kubeconfig (``$KUBECONFIG`` or
  ``~/.kube/config``); the namespace is taken from ``$PRIMER_K8S_NAMESPACE``
  (default ``primer``).

Skips gracefully when either precondition is absent so it is inert in CI
without a cluster. The coordinator runs this against the k3s setup
(see the k8s in-cluster platform memory).

What it proves: ``list()`` enumerates workspaces by the
``app.kubernetes.io/managed-by=primer`` label selector off the live
StatefulSets -- i.e. it reflects what is actually in the cluster (durable
across the API/worker split + platform restarts), not just this process's
in-memory handles. It does NOT create workspaces (that needs the full
in-cluster runtime image + platform wiring the coordinator owns); it
asserts the query path works and the result type is correct.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip(
    "kubernetes_asyncio", reason="kubernetes-asyncio not installed",
)


def _cluster_reachable() -> bool:
    try:
        from kubernetes_asyncio import client, config as kconfig

        async def _check() -> bool:
            try:
                await kconfig.load_kube_config()
            except Exception:
                try:
                    kconfig.load_incluster_config()
                except Exception:
                    return False
            api = client.CoreV1Api(client.ApiClient())
            try:
                await api.list_namespace(limit=1)
                return True
            except Exception:
                return False
            finally:
                await api.api_client.close()

        return asyncio.run(_check())
    except Exception:
        return False


_CLUSTER = _cluster_reachable()

pytestmark = pytest.mark.skipif(
    not _CLUSTER,
    reason="no reachable k8s cluster via default kubeconfig / in-cluster config",
)


def _namespace() -> str:
    return os.environ.get("PRIMER_K8S_NAMESPACE", "primer")


def _kubeconfig_path() -> str:
    return os.environ.get(
        "KUBECONFIG", os.path.expanduser("~/.kube/config"),
    )


def _config():
    from primer.model.workspace import (
        K8sConnectionKubeconfig,
        K8sReachabilityInCluster,
        KubernetesWorkspaceConfig,
    )
    return KubernetesWorkspaceConfig(
        connection=K8sConnectionKubeconfig(path=_kubeconfig_path()),
        namespace=_namespace(),
        reachability=K8sReachabilityInCluster(),
    )


@pytest.mark.asyncio
async def test_list_queries_live_cluster() -> None:
    """list() returns the set of workspace ids derived from the live
    primer-managed StatefulSets in the namespace -- a plain list[str]."""
    from primer.workspace.k8s.backend import KubernetesWorkspaceBackend

    backend = KubernetesWorkspaceBackend(_config())
    await backend.initialize()
    try:
        ids = await backend.list()
        assert isinstance(ids, list)
        assert all(isinstance(i, str) for i in ids)
        # Idempotent: a second call returns the same set.
        ids2 = await backend.list()
        assert sorted(ids) == sorted(ids2)
    finally:
        await backend.aclose()
