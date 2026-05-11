"""KubernetesWorkspaceBackend -- one StatefulSet + PVC per workspace.

Speaks the K8s API via ``kubernetes-asyncio`` directly. Construction
of the K8s client is deferred to :meth:`initialize` so the module
loads cleanly even when ``kubernetes-asyncio`` isn't usable.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from typing import Any

from matrix.int.workspace import Workspace, WorkspaceBackend
from matrix.model.except_ import ConfigError, NotFoundError
from matrix.model.workspace import (
    KubernetesWorkspaceConfig,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
    _KubernetesTemplateConfig,
)
from matrix.workspace.k8s.sandbox import K8sSandbox
from matrix.workspace.sandbox.workspace import SandboxWorkspace


logger = logging.getLogger(__name__)


def _generate_workspace_id() -> str:
    return f"ws-{uuid.uuid4().hex[:16]}"


def _pvc_name_for(sts_name: str) -> str:
    """StatefulSet's volumeClaimTemplate name is ``ws``; K8s names PVCs
    ``<vct>-<sts>-<ordinal>``. For replicas=1 the only PVC is index 0."""
    return f"ws-{sts_name}-0"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge ``overlay`` onto ``base`` per the workspace-backends spec:
    scalars overlay (overlay wins), dicts recurse, lists extend (base
    first), ``None`` in overlay removes the base key."""
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if v is None and k in out:
            del out[k]
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = list(out[k]) + list(v)
        else:
            out[k] = v
    return out


def _build_statefulset_manifest(
    *,
    sts_name: str,
    namespace: str,
    workspace_id: str,
    template: WorkspaceTemplate,
    provider_cfg: KubernetesWorkspaceConfig,
) -> dict[str, Any]:
    """Compose the StatefulSet body with the template's recipe + any
    container/pod overrides deep-merged on top."""
    assert isinstance(template.backend, _KubernetesTemplateConfig)
    tcfg = template.backend
    base_container: dict[str, Any] = {
        "name": "workspace",
        "image": tcfg.image,
        "workingDir": tcfg.workdir,
        "imagePullPolicy": provider_cfg.pull_policy,
        "volumeMounts": [
            {"name": "ws", "mountPath": tcfg.workdir},
        ] + list(tcfg.extra_volume_mounts),
        "env": [
            {"name": k, "value": v.get_secret_value()}
            for k, v in template.env.items()
        ],
    }
    if tcfg.entrypoint is not None:
        base_container["command"] = list(tcfg.entrypoint)
    else:
        base_container["command"] = ["sleep", "infinity"]
    if tcfg.args is not None:
        base_container["args"] = list(tcfg.args)
    if provider_cfg.container_security_context is not None:
        base_container["securityContext"] = copy.deepcopy(
            provider_cfg.container_security_context,
        )
    if template.resources.cpu_cores is not None or template.resources.memory_bytes is not None:
        limits: dict[str, str] = {}
        if template.resources.cpu_cores is not None:
            limits["cpu"] = str(template.resources.cpu_cores)
        if template.resources.memory_bytes is not None:
            limits["memory"] = str(template.resources.memory_bytes)
        base_container["resources"] = {"limits": limits, "requests": limits}

    # Apply container_overrides via deep merge.
    container = _deep_merge(base_container, tcfg.container_overrides)

    base_pod: dict[str, Any] = {
        "containers": [container],
        "volumes": list(tcfg.extra_volumes),
    }
    if provider_cfg.service_account is not None:
        base_pod["serviceAccountName"] = provider_cfg.service_account
    if provider_cfg.image_pull_secrets:
        base_pod["imagePullSecrets"] = [
            {"name": n} for n in provider_cfg.image_pull_secrets
        ]
    if provider_cfg.node_selector:
        base_pod["nodeSelector"] = dict(provider_cfg.node_selector)
    if provider_cfg.tolerations:
        base_pod["tolerations"] = list(provider_cfg.tolerations)
    if provider_cfg.pod_security_context is not None:
        base_pod["securityContext"] = copy.deepcopy(
            provider_cfg.pod_security_context,
        )

    pod = _deep_merge(base_pod, tcfg.pod_overrides)

    labels = {
        "matrix.workspace.id": workspace_id,
        **provider_cfg.labels,
    }
    annotations = dict(provider_cfg.annotations)

    pvc_spec: dict[str, Any] = {
        "accessModes": list(tcfg.pvc_access_modes),
        "resources": {"requests": {"storage": tcfg.pvc_size}},
    }
    if provider_cfg.storage_class is not None:
        pvc_spec["storageClassName"] = provider_cfg.storage_class

    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": sts_name,
            "namespace": namespace,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {
            "replicas": 1,
            "serviceName": f"{sts_name}-headless",
            "selector": {"matchLabels": {"matrix.workspace.id": workspace_id}},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": annotations,
                },
                "spec": pod,
            },
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "ws"},
                    "spec": pvc_spec,
                },
            ],
        },
    }


class KubernetesWorkspaceBackend(WorkspaceBackend):
    """Materialises workspaces as single-replica StatefulSets."""

    def __init__(
        self,
        config: KubernetesWorkspaceConfig,
        *,
        core_v1=None,
        apps_v1=None,
        ws_api=None,
    ) -> None:
        self._config = config
        self._core_v1 = core_v1
        self._apps_v1 = apps_v1
        self._ws_api = ws_api
        self._workspaces: dict[str, SandboxWorkspace] = {}
        self._lock = asyncio.Lock()
        self._initialised = False

    async def initialize(self) -> None:
        if self._initialised:
            return
        if self._core_v1 is None or self._apps_v1 is None:
            from kubernetes_asyncio import client, config as kconfig

            if self._config.in_cluster:
                kconfig.load_incluster_config()
            else:
                await kconfig.load_kube_config(
                    config_file=self._config.kubeconfig_path,
                    context=self._config.context,
                )
            api_client = client.ApiClient()
            self._core_v1 = client.CoreV1Api(api_client)
            self._apps_v1 = client.AppsV1Api(api_client)
        self._initialised = True

    async def aclose(self) -> None:
        async with self._lock:
            for ws in list(self._workspaces.values()):
                try:
                    await ws.aclose()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("aclose on workspace failed: %s", exc)
            self._workspaces.clear()
            self._initialised = False

    async def create(
        self,
        template: WorkspaceTemplate,
        *,
        overrides: WorkspaceTemplateOverrides | None = None,
    ) -> Workspace:
        if not isinstance(template.backend, _KubernetesTemplateConfig):
            raise ConfigError(
                f"KubernetesWorkspaceBackend requires template backend kind "
                f"'kubernetes', got {template.backend.kind!r}"
            )
        if not self._initialised:
            await self.initialize()
        assert self._core_v1 is not None and self._apps_v1 is not None

        workspace_id = _generate_workspace_id()
        sts_name = f"{self._config.name_prefix}{workspace_id}"
        pvc_name = _pvc_name_for(sts_name)

        manifest = _build_statefulset_manifest(
            sts_name=sts_name,
            namespace=self._config.namespace,
            workspace_id=workspace_id,
            template=template,
            provider_cfg=self._config,
        )
        try:
            await self._apps_v1.create_namespaced_stateful_set(
                self._config.namespace, manifest,
            )
        except Exception:
            raise

        # Wait for the Pod to be Running (pod name is <sts>-0).
        pod_name = f"{sts_name}-0"
        await self._wait_for_pod_running(pod_name)

        sandbox = K8sSandbox(
            core_v1=self._core_v1,
            apps_v1=self._apps_v1,
            ws_api=self._ws_api,
            namespace=self._config.namespace,
            sts_name=sts_name,
            pod_name=pod_name,
            sandbox_id=sts_name,
            pvc_name=pvc_name,
        )

        try:
            ws = await SandboxWorkspace.materialise(
                workspace_id=workspace_id,
                template=template,
                sandbox=sandbox,
                backend_kind="kubernetes",
                workspace_root=template.backend.workdir,
            )
        except Exception:
            try:
                await sandbox.remove()
            except Exception as exc:  # noqa: BLE001
                logger.warning("rollback remove failed: %s", exc)
            raise

        async with self._lock:
            self._workspaces[workspace_id] = ws
        return ws

    async def _wait_for_pod_running(
        self, pod_name: str, *, timeout_seconds: float = 120.0,
    ) -> None:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        assert self._core_v1 is not None
        while True:
            try:
                pod = await self._core_v1.read_namespaced_pod(
                    pod_name, self._config.namespace,
                )
                phase = pod.status.phase if pod.status else None
                if phase == "Running":
                    return
                if phase in ("Failed", "Succeeded"):
                    raise ConfigError(
                        f"Pod {pod_name!r} reached phase {phase!r} before Running"
                    )
            except Exception as exc:  # noqa: BLE001
                if "404" not in str(exc) and asyncio.get_event_loop().time() > deadline:
                    raise
            if asyncio.get_event_loop().time() > deadline:
                raise ConfigError(
                    f"timed out waiting for Pod {pod_name!r} to enter Running"
                )
            await asyncio.sleep(1.0)

    async def get(self, workspace_id: str) -> Workspace | None:
        return self._workspaces.get(workspace_id)

    async def list(self) -> list[str]:
        return list(self._workspaces)

    async def destroy(self, workspace_id: str) -> None:
        async with self._lock:
            ws = self._workspaces.pop(workspace_id, None)
        if ws is None:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        sandbox = ws._sandbox  # noqa: SLF001
        await sandbox.stop()
        await sandbox.remove()


__all__ = ["KubernetesWorkspaceBackend"]
