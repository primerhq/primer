"""KubernetesWorkspaceBackend -- one StatefulSet + PVC per workspace.

Speaks the K8s API via ``kubernetes-asyncio`` directly. Construction
of the K8s client is deferred to :meth:`initialize` so the module
loads cleanly even when ``kubernetes-asyncio`` isn't usable.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
import uuid
from typing import Any

from primer.int.workspace import Workspace, WorkspaceBackend
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.workspace import (
    KubernetesWorkspaceConfig,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
    KubernetesTemplateConfig,
)
from primer.workspace.files import resolve_file_sources
from primer.workspace.k8s.sandbox import K8sSandbox
from primer.workspace.sandbox.workspace import SandboxWorkspace


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


# Keys disallowed in any per-template ``container_overrides`` /
# ``pod_overrides`` overlay because they would let a template author
# escalate to host root or break out of the sandbox. Matched on any
# nesting level via :func:`_assert_no_dangerous_keys`.
_FORBIDDEN_OVERRIDE_KEYS = frozenset({
    "securityContext",     # privileged: true, runAsUser: 0, capabilities, etc.
    "hostPath",            # mount the host filesystem
    "hostNetwork",         # share the host's network namespace
    "hostPID",             # share the host's PID namespace
    "hostIPC",             # share the host's IPC namespace
    "hostUsers",
    "privileged",
    "allowPrivilegeEscalation",
    "capabilities",
    "procMount",
    "runAsUser",
    "runAsGroup",
    "runAsNonRoot",
})


def _assert_no_dangerous_keys(
    overlay: Any, *, source: str, path: str = "",
) -> None:
    """Refuse template overlays that try to set security-sensitive K8s
    fields. The check walks dicts and lists recursively so nested
    occurrences (``securityContext`` inside an ``initContainers`` entry,
    ``hostPath`` inside an ``extra_volumes`` element, …) are caught.

    Raises :class:`ConfigError` with a message naming the offending key
    and its dotted path.
    """
    if isinstance(overlay, dict):
        for k, v in overlay.items():
            if k in _FORBIDDEN_OVERRIDE_KEYS:
                raise ConfigError(
                    f"{source} sets disallowed K8s field {k!r} "
                    f"(at {path or '<root>'}.{k}); refused for safety."
                )
            _assert_no_dangerous_keys(v, source=source, path=f"{path}.{k}")
    elif isinstance(overlay, list):
        for i, item in enumerate(overlay):
            _assert_no_dangerous_keys(
                item, source=source, path=f"{path}[{i}]",
            )


def _validate_template_overrides(template: WorkspaceTemplate) -> None:
    """Reject security-sensitive K8s fields in the template's overlay
    dicts. Called before the manifest is built so a malicious template
    fails loudly at create time rather than silently producing a
    privileged Pod."""
    if not isinstance(template.backend, KubernetesTemplateConfig):
        return
    tcfg = template.backend
    _assert_no_dangerous_keys(
        tcfg.pod_overrides or {}, source="pod_overrides",
    )
    _assert_no_dangerous_keys(
        [vol.model_dump(exclude_none=True) if hasattr(vol, "model_dump") else vol for vol in tcfg.extra_volumes],
        source="extra_volumes",
    )
    _assert_no_dangerous_keys(
        [vm.model_dump(exclude_none=True) if hasattr(vm, "model_dump") else vm for vm in tcfg.extra_volume_mounts],
        source="extra_volume_mounts",
    )


def _build_statefulset_manifest(
    *,
    sts_name: str,
    namespace: str,
    workspace_id: str,
    template: WorkspaceTemplate,
    provider_cfg: KubernetesWorkspaceConfig,
    obj_name: str | None = None,
) -> dict[str, Any]:
    """Compose the StatefulSet body for a workspace Pod.

    ``obj_name`` is the per-workspace object name (Secret + Headless
    Service share it; see :func:`primer.workspace.k8s.naming.k8s_object_name`).
    The container ``envFrom``s this Secret to inherit ``RUNTIME_TOKEN``,
    and ``spec.serviceName`` is set to ``obj_name`` so the STS binds to
    the matching Headless Service. ``workspace-id=<id>`` lands on the pod
    template labels to match the Service selector.
    """
    assert isinstance(template.backend, KubernetesTemplateConfig)
    tcfg = template.backend
    if obj_name is None:
        from primer.workspace.k8s.naming import k8s_object_name
        obj_name = k8s_object_name(workspace_id)

    base_container: dict[str, Any] = {
        "name": "workspace",
        "image": tcfg.image,
        "workingDir": tcfg.workdir,
        "volumeMounts": [
            {"name": "ws", "mountPath": tcfg.workdir},
        ] + [vm.model_dump(exclude_none=True) if hasattr(vm, "model_dump") else dict(vm) for vm in tcfg.extra_volume_mounts],
        "env": [
            {"name": k, "value": v.get_secret_value()}
            for k, v in template.env.items()
        ],
        "envFrom": [
            {"secretRef": {"name": obj_name}},
        ],
        "ports": [
            {"name": "runtime", "containerPort": 5959},
        ],
    }
    if tcfg.entrypoint is not None:
        base_container["command"] = list(tcfg.entrypoint)
    else:
        base_container["command"] = ["sleep", "infinity"]
    if tcfg.args is not None:
        base_container["args"] = list(tcfg.args)
    if template.resources.cpu_cores is not None or template.resources.memory_bytes is not None:
        limits: dict[str, str] = {}
        if template.resources.cpu_cores is not None:
            limits["cpu"] = str(template.resources.cpu_cores)
        if template.resources.memory_bytes is not None:
            limits["memory"] = str(template.resources.memory_bytes)
        base_container["resources"] = {"limits": limits, "requests": limits}

    container = base_container

    base_pod: dict[str, Any] = {
        "containers": [container],
        "volumes": [v.model_dump(exclude_none=True) if hasattr(v, "model_dump") else dict(v) for v in tcfg.extra_volumes],
    }
    if provider_cfg.image_pull_secrets:
        base_pod["imagePullSecrets"] = [
            {"name": n} for n in provider_cfg.image_pull_secrets
        ]

    pod = _deep_merge(base_pod, tcfg.pod_overrides or {})

    labels = {
        "primer.workspace.id": workspace_id,
        "workspace-id": workspace_id,
        "app.kubernetes.io/managed-by": "primer",
    }
    annotations: dict[str, str] = {}

    pvc_spec: dict[str, Any] = {
        "accessModes": list(tcfg.pvc_access_modes),
        "resources": {"requests": {"storage": tcfg.pvc_size}},
    }
    if tcfg.storage_class is not None:
        pvc_spec["storageClassName"] = tcfg.storage_class

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
            "serviceName": obj_name,
            "selector": {"matchLabels": {"workspace-id": workspace_id}},
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
        if not isinstance(template.backend, KubernetesTemplateConfig):
            raise ConfigError(
                f"KubernetesWorkspaceBackend requires template backend kind "
                f"'kubernetes', got {template.backend.kind!r}"
            )
        # Refuse templates that try to set security-sensitive K8s fields
        # via the override passthrough dicts (privileged, hostPath, ...).
        _validate_template_overrides(template)
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
            # Resolve every FileSource variant (inline/url/document/secret)
            # up-front via the central helper; the sandbox just writes the
            # resulting bytes. document/secret resolvers aren't wired here
            # yet — the orchestration layer will pass them in once Phase 6
            # threads app state through.
            files = list(template.files) + (
                list(overrides.files) if overrides else []
            )
            resolved_files = await resolve_file_sources(files)
            workdir = template.backend.workdir
            for rf in resolved_files:
                await sandbox.write_file(
                    f"{workdir}/{rf.path}",
                    rf.content,
                )
                # NOTE: file mode application via exec is deferred;
                # the sandbox protocol has no chmod yet (Phase 5 will add it).
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

    async def _create_secret(
        self, workspace_id: str, obj_name: str,
    ) -> str:
        """Create the per-workspace Secret holding ``RUNTIME_TOKEN``.

        Returns the freshly-generated bearer token so the caller can both
        wire it into the StatefulSet (``envFrom`` -> Secret) and hand it
        to the platform-side ``RuntimeClient``. The token never leaves
        this process except via the Secret object, which lives in the
        same namespace as the workspace Pod.
        """
        token = secrets.token_urlsafe(32)
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": obj_name,
                "namespace": self._config.namespace,
                "labels": {
                    "workspace-id": workspace_id,
                    "app.kubernetes.io/managed-by": "primer",
                },
            },
            "stringData": {"RUNTIME_TOKEN": token},
        }
        assert self._core_v1 is not None
        await self._core_v1.create_namespaced_secret(
            namespace=self._config.namespace,
            body=body,
        )
        return token

    async def _create_service(
        self, workspace_id: str, obj_name: str,
    ) -> None:
        """Create a Headless Service (``clusterIP: None``) selecting the
        workspace's Pod via ``workspace-id=<id>``.

        Gives each Pod a stable DNS name of the form
        ``<obj_name>-0.<obj_name>.<ns>.svc.cluster.local`` -- required by
        the in-cluster reachability mode so the platform can reach the
        workspace-runtime without relying on Pod IPs.

        Idempotent enough for retry: assumes the caller catches
        ``Conflict`` (409) on re-create.
        """
        assert self._core_v1 is not None
        body = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": obj_name,
                "namespace": self._config.namespace,
                "labels": {
                    "workspace-id": workspace_id,
                    "app.kubernetes.io/managed-by": "primer",
                },
            },
            "spec": {
                "clusterIP": "None",  # headless
                "selector": {"workspace-id": workspace_id},
                "ports": [
                    {"name": "runtime", "port": 5959, "targetPort": 5959},
                ],
            },
        }
        await self._core_v1.create_namespaced_service(
            namespace=self._config.namespace,
            body=body,
        )

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

    async def get(
        self,
        workspace_id: str,
        *,
        template: WorkspaceTemplate | None = None,
    ) -> Workspace | None:
        cached = self._workspaces.get(workspace_id)
        if cached is not None:
            return cached
        # Re-attach: the StatefulSet may exist from a previous process.
        # We need a template to materialise the SandboxWorkspace wrapper;
        # without one, return None and let the API layer re-issue with
        # the template loaded from storage.
        if template is None:
            return None
        if not isinstance(template.backend, KubernetesTemplateConfig):
            raise ConfigError(
                f"re-attach for workspace {workspace_id!r}: template "
                f"backend kind is {template.backend.kind!r}, expected "
                "'kubernetes'"
            )
        if not self._initialised:
            await self.initialize()
        assert self._core_v1 is not None and self._apps_v1 is not None
        sts_name = f"{self._config.name_prefix}{workspace_id}"
        try:
            await self._apps_v1.read_namespaced_stateful_set(
                sts_name, self._config.namespace,
            )
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc):
                return None
            raise
        pod_name = f"{sts_name}-0"
        # Make sure the Pod is up (the StatefulSet may have been scaled
        # to 0 by a prior `K8sSandbox.stop`).
        try:
            await self._wait_for_pod_running(pod_name)
        except ConfigError:
            # Scaled down -- bring it back up.
            await self._apps_v1.patch_namespaced_stateful_set_scale(
                sts_name, self._config.namespace,
                {"spec": {"replicas": 1}},
            )
            await self._wait_for_pod_running(pod_name)
        sandbox = K8sSandbox(
            core_v1=self._core_v1,
            apps_v1=self._apps_v1,
            ws_api=self._ws_api,
            namespace=self._config.namespace,
            sts_name=sts_name,
            pod_name=pod_name,
            sandbox_id=sts_name,
            pvc_name=_pvc_name_for(sts_name),
        )
        ws = await SandboxWorkspace.materialise(
            workspace_id=workspace_id,
            template=template,
            sandbox=sandbox,
            backend_kind="kubernetes",
            workspace_root=template.backend.workdir,
        )
        async with self._lock:
            existing = self._workspaces.get(workspace_id)
            if existing is not None:
                return existing
            self._workspaces[workspace_id] = ws
        return ws

    async def list(self) -> list[str]:
        return list(self._workspaces)

    async def destroy(self, workspace_id: str) -> None:
        async with self._lock:
            ws = self._workspaces.pop(workspace_id, None)
        if ws is None:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        sandbox = ws.sandbox
        await sandbox.stop()
        await sandbox.remove()


__all__ = ["KubernetesWorkspaceBackend"]
