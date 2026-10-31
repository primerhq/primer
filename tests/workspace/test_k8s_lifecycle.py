"""Kubernetes workspace lifecycle: a destroy whose StatefulSet is already gone, and closing the backend (review W51, W20).

* W51: ``destroy`` raised ``NotFoundError`` as soon as the StatefulSet delete answered 404, before the Service, the Secret
  and the PVC were deleted, so they were orphaned and a same-id create hit a 409 on the Secret. A failed PVC delete was only
  logged, so the row went while the data stayed. The best-effort deletes now run first, and a PVC that could not be deleted
  fails the destroy.
* W20: closing the backend released each cached workspace's handle but not its runtime connection, never closed the
  Kubernetes ``ApiClient`` it built, and left the service-account CA bundle in a temp file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.model.except_ import NotFoundError
from primer.model.workspace import (
    K8sConnectionServiceAccountToken,
    K8sReachabilityInCluster,
    KubernetesWorkspaceConfig,
    WorkspaceRuntimeMeta,
)
from primer.workspace.k8s.backend import KubernetesWorkspaceBackend
from primer.workspace.runtime.ws_sandbox import WSSandbox
from primer.workspace.sandbox.workspace import SandboxWorkspace
from tests.workspace.test_k8s_client_is_closed_when_the_build_does_not_finish import _backend


def _destroying_backend(*, sts_error: Exception | None = None, pvc_error: Exception | None = None,
                        service_error: Exception | None = None) -> KubernetesWorkspaceBackend:
    backend = _backend()
    backend._custom_objects = None
    backend._apps_v1.delete_namespaced_stateful_set = AsyncMock(side_effect=sts_error)
    backend._core_v1.delete_namespaced_service = AsyncMock(side_effect=service_error)
    backend._core_v1.delete_namespaced_secret = AsyncMock()
    backend._core_v1.delete_namespaced_persistent_volume_claim = AsyncMock(side_effect=pvc_error)
    return backend


def _deleted(backend: KubernetesWorkspaceBackend) -> dict[str, list[str]]:
    calls = {
        "statefulset": backend._apps_v1.delete_namespaced_stateful_set,
        "service": backend._core_v1.delete_namespaced_service,
        "secret": backend._core_v1.delete_namespaced_secret,
        "pvc": backend._core_v1.delete_namespaced_persistent_volume_claim,
    }
    return {kind: [c.kwargs["name"] for c in mock.await_args_list] for kind, mock in calls.items()}


# ---- characterization ------------------------------------------------------------------------------------------------------------


async def test_a_complete_set_is_deleted() -> None:
    backend = _destroying_backend()
    await backend.destroy("ws-1")
    assert _deleted(backend) == {
        "statefulset": ["primer-ws-ws-1"], "service": ["primer-ws-ws-1"], "secret": ["primer-ws-ws-1"],
        "pvc": ["ws-primer-ws-ws-1-0"],
    }


async def test_a_service_that_cannot_be_deleted_does_not_fail_the_destroy() -> None:
    backend = _destroying_backend(service_error=Exception("(500) Internal Server Error"))
    await backend.destroy("ws-1")
    assert _deleted(backend)["pvc"] == ["ws-primer-ws-ws-1-0"]


async def test_a_pvc_that_is_already_gone_is_fine() -> None:
    backend = _destroying_backend(pvc_error=Exception("(404) Not Found"))
    await backend.destroy("ws-1")


async def test_a_statefulset_delete_that_fails_otherwise_raises() -> None:
    backend = _destroying_backend(sts_error=Exception("(500) Internal Server Error"))
    with pytest.raises(Exception, match="500"):
        await backend.destroy("ws-1")


# ---- W20 -------------------------------------------------------------------------------------------------------------------------


async def test_closing_the_backend_closes_each_cached_runtime_connection() -> None:
    backend = _backend()
    client = MagicMock()
    client.aclose = AsyncMock()
    client.gone = False
    backend._workspaces["w"] = SandboxWorkspace(
        workspace_id="w", template=MagicMock(), sandbox=WSSandbox(runtime_client=client, container_id="c"),
        state_repo=MagicMock(), truncation_store=MagicMock(), tools=[], backend_kind="kubernetes",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"),
    )

    await backend.aclose()

    client.aclose.assert_awaited_once()
    assert backend._workspaces == {}


async def test_closing_the_backend_closes_its_api_client_and_removes_the_ca_file(monkeypatch) -> None:
    import kubernetes_asyncio.client as kclient

    made: list = []

    class _ApiClient:
        def __init__(self, configuration=None) -> None:
            self.configuration = configuration
            self.closed = 0
            made.append(self)

        async def close(self) -> None:
            self.closed += 1

    monkeypatch.setattr(kclient, "ApiClient", _ApiClient)
    for api in ("CoreV1Api", "AppsV1Api", "CustomObjectsApi"):
        monkeypatch.setattr(kclient, api, lambda api_client: MagicMock())
    backend = KubernetesWorkspaceBackend(KubernetesWorkspaceConfig(
        connection=K8sConnectionServiceAccountToken(
            apiserver_url="https://k8s.example:6443", token="tok",
            ca_data="-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
        ),
        namespace="primer-ns", reachability=K8sReachabilityInCluster(),
    ))
    await backend.initialize()
    (api_client,) = made
    ca_file = Path(api_client.configuration.ssl_ca_cert)
    assert ca_file.exists()

    await backend.aclose()

    assert api_client.closed == 1
    assert not ca_file.exists()
