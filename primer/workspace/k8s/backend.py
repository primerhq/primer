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
from collections.abc import Awaitable
from typing import Any

import aiohttp

from primer.int.workspace import Workspace
from primer.model.except_ import ConfigError, NotFoundError
from pydantic import SecretStr

from primer.model.workspace import (
    K8sConnectionInCluster,
    K8sConnectionKubeconfig,
    K8sConnectionServiceAccountToken,
    K8sReachabilityGateway,
    KubernetesWorkspaceConfig,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
    KubernetesTemplateConfig,
)
from primer.workspace.base_backend import BaseWorkspaceBackend, close_shielded, roll_back_shielded
from primer.workspace.files import FileResolvers
from primer.workspace.k8s.httproute import build_httproute_manifest
from primer.workspace.k8s.naming import k8s_object_name
from primer.workspace.runtime.runtime_client import RuntimeClient
from primer.workspace.runtime.url import build_runtime_url
from primer.workspace.runtime.ws_sandbox import WSSandbox
from primer.workspace.sandbox.workspace import SandboxWorkspace


logger = logging.getLogger(__name__)

# How long a Running pod is given for its runtime to start listening (ticket 01a11543). A pod is Running once its container has
# STARTED, which is before the runtime process serves; the Docker adapter allows 30 s for its ready marker, a pod on a loaded node
# is slower to start a Python process, and the gateway route may need a moment too.
_RUNTIME_READY_TIMEOUT_S = 60.0
_RUNTIME_READY_POLL_S = 0.25        # the first pause between attempts; it doubles up to the cap below
_RUNTIME_READY_MAX_POLL_S = 2.0

# What a gateway answers while it has not programmed or reached a freshly created backend.
_GATEWAY_NOT_READY_STATUSES = frozenset({404, 502, 503, 504})


def _runtime_not_serving_yet(exc: BaseException) -> bool:
    """True when a failed connect means "the runtime is not serving yet", so the same connect may succeed a moment later.

    A refused or dropped connection, a hello that timed out, and a gateway that answers 404/502/503/504 for a backend it has not
    reached yet. NOT a 401/403 (the token is wrong: waiting cannot fix it), a TLS failure, a protocol error (``RuntimeError`` from
    the hello) or anything else: those fail at once with their own error.
    """
    if isinstance(exc, aiohttp.WSServerHandshakeError):
        return exc.status in _GATEWAY_NOT_READY_STATUSES
    if isinstance(exc, aiohttp.ClientSSLError):
        return False
    return isinstance(exc, (aiohttp.ClientConnectionError, ConnectionError, TimeoutError))


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
    if template.strict_write_locking:
        base_container["env"].append(
            {"name": "PRIMER_STRICT_WRITE_LOCKING", "value": "1"}
        )
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
        # The container always gets a ``command`` above, and a Kubernetes ``command`` replaces the image's ENTRYPOINT,
        # so the init baked into the runtime image (tini) never runs in a workspace pod. With a shared process
        # namespace the pod's pause container is PID 1 and reaps the processes an exec leaves orphaned (without an init
        # they stay zombies for the life of the pod). A template can turn it off through ``pod_overrides``.
        "shareProcessNamespace": True,
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


class KubernetesWorkspaceBackend(BaseWorkspaceBackend):
    """Materialises workspaces as single-replica StatefulSets."""

    def __init__(
        self,
        config: KubernetesWorkspaceConfig,
        *,
        core_v1=None,
        apps_v1=None,
        custom_objects=None,
        ws_api=None,
    ) -> None:
        super().__init__()
        self._config = config
        self._core_v1 = core_v1
        self._apps_v1 = apps_v1
        self._custom_objects = custom_objects
        self._ws_api = ws_api
        self._workspaces: dict[str, SandboxWorkspace] = {}

    async def initialize(self) -> None:
        if self._initialised:
            return
        if self._core_v1 is None or self._apps_v1 is None or self._custom_objects is None:
            from kubernetes_asyncio import client, config as kconfig

            conn = self._config.connection
            if isinstance(conn, K8sConnectionInCluster):
                kconfig.load_incluster_config()
                api_client = client.ApiClient()
            elif isinstance(conn, K8sConnectionKubeconfig):
                await kconfig.load_kube_config(
                    config_file=conn.path,
                    context=conn.context,
                )
                api_client = client.ApiClient()
            elif isinstance(conn, K8sConnectionServiceAccountToken):
                cfg = client.Configuration()
                cfg.host = conn.apiserver_url
                cfg.api_key = {
                    "authorization": f"Bearer {conn.token.get_secret_value()}",
                }
                # Trust the supplied PEM bundle for the apiserver.
                import tempfile
                ca_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".pem", delete=False,
                )
                ca_file.write(conn.ca_data)
                ca_file.close()
                cfg.ssl_ca_cert = ca_file.name
                api_client = client.ApiClient(configuration=cfg)
            else:
                raise ConfigError(
                    f"unknown k8s connection kind {conn.kind!r}"
                )
            if self._core_v1 is None:
                self._core_v1 = client.CoreV1Api(api_client)
            if self._apps_v1 is None:
                self._apps_v1 = client.AppsV1Api(api_client)
            if self._custom_objects is None:
                self._custom_objects = client.CustomObjectsApi(api_client)
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
        workspace_id: str | None = None,
        resolvers: FileResolvers | None = None,
    ) -> Workspace:
        """Materialise a workspace as Secret + Headless Service + StatefulSet
        and wire a :class:`SandboxWorkspace` over a :class:`WSSandbox` over
        :class:`RuntimeClient`.

        ``workspace_id`` is generated when not supplied so this matches the
        :class:`WorkspaceBackend` ABC signature; callers (and tests) may pin
        the id for predictable K8s object names.
        """
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

        if workspace_id is None:
            workspace_id = _generate_workspace_id()
        obj_name = k8s_object_name(workspace_id)

        # What THIS create has made in the cluster, so a rollback deletes only that (never the objects of a live workspace that
        # already holds the name: its Secret create answers 409 and nothing is recorded).
        created: list[str] = []
        try:
            return await self._build(
                template, overrides=overrides, workspace_id=workspace_id, obj_name=obj_name, resolvers=resolvers,
                created=created,
            )
        except BaseException:
            # BaseException, not Exception: a cancel or a caller's ``asyncio.timeout`` (the pod wait is up to 2 minutes) left
            # the Secret, Service, StatefulSet and its PVC behind with nothing pointing at them, and so did a plain failure
            # after the first object (a StatefulSet the API refused, a pod that never ran, an init command that failed).
            # Nothing else reclaims them: no orphan sweep, and a caller that did not pin the id never learns the generated
            # one. The rollback runs on its own task and the wait for it is bounded: see ``roll_back_shielded``.
            if created:
                await roll_back_shielded(
                    self._roll_back_create(obj_name, created), what=f"k8s create rollback of {workspace_id}",
                )
            raise

    @staticmethod
    async def _tracked(created: list[str], kind: str, call: Awaitable[Any]) -> Any:
        """Await ``call`` (the create of one cluster object) and record ``kind`` in ``created``.

        Recorded BEFORE the request is awaited: a cancel or a timeout that lands while it is in flight may have created the
        object server side. Withdrawn again only when the API server DEFINITIVELY refused it: an error carrying a 4xx
        ``status`` (a 409 because a live workspace already holds the name, a 403, a 422). A client-side timeout or a dropped
        connection is not a refusal (the request may have been carried out), so the record stays and the rollback's delete,
        which tolerates a 404, is the safe side.
        """
        created.append(kind)
        try:
            return await call
        except Exception as exc:
            status = getattr(exc, "status", None)
            if isinstance(status, int) and 400 <= status < 500:
                created.remove(kind)
            raise

    async def _build(
        self,
        template: WorkspaceTemplate,
        *,
        overrides: WorkspaceTemplateOverrides | None,
        workspace_id: str,
        obj_name: str,
        resolvers: FileResolvers | None,
        created: list[str],
    ) -> Workspace:
        """The body of :meth:`create`: the cluster objects, the pod wait, the runtime connection. ``created`` is filled in as
        the objects are made, for the rollback."""
        assert self._core_v1 is not None and self._apps_v1 is not None

        # 1. Per-workspace Secret with RUNTIME_TOKEN -- the STS will envFrom
        #    this Secret so the runtime container inherits the bearer token
        #    on start-up.
        token = await self._tracked(created, "secret", self._create_secret(workspace_id, obj_name))
        # 2. Headless Service -- gives the Pod stable DNS
        #    (<obj_name>-0.<obj_name>.<ns>.svc.cluster.local).
        await self._tracked(created, "service", self._create_service(workspace_id, obj_name))
        # 3. StatefulSet bound to the Service (serviceName=obj_name) with
        #    envFrom the Secret and workspace-id label on the pod template
        #    so the Service selector matches.
        manifest = _build_statefulset_manifest(
            sts_name=obj_name,
            namespace=self._config.namespace,
            workspace_id=workspace_id,
            template=template,
            provider_cfg=self._config,
            obj_name=obj_name,
        )
        await self._tracked(created, "statefulset", self._apps_v1.create_namespaced_stateful_set(
            namespace=self._config.namespace,
            body=manifest,
        ))

        # 3b. For gateway_httproute reachability, create the per-workspace
        #     HTTPRoute so the platform can dial the pod through the Gateway.
        #     Done before the pod-wait so a route failure surfaces fast. A
        #     failure rolls back the Secret/Service/StatefulSet just created
        #     (in ``create``) so a bad route does not orphan workspace objects.
        if isinstance(self._config.reachability, K8sReachabilityGateway):
            await self._tracked(created, "httproute", self._create_httproute(workspace_id, obj_name))

        # 4. Wait for the Pod to be Running (pod name is <sts>-0).
        pod_name = f"{obj_name}-0"
        await self._wait_for_pod_running(pod_name)

        # 5. Open the runtime WebSocket and wrap as a Sandbox + Workspace.
        url = build_runtime_url(
            provider_config=self._config,
            workspace_id=workspace_id,
            k8s_object_name=obj_name,
        )
        client = await self._connect_runtime(url, token, workspace_id=workspace_id)
        cached = False
        try:
            sandbox = WSSandbox(
                runtime_client=client,
                container_id=obj_name,
                workspace_root=template.backend.workdir,
            )
            # Resolve every FileSource variant (inline/url/document/secret)
            # up-front via the shared helper; the sandbox writes the
            # resulting bytes via the WS runtime.
            merged = self.merge_overrides(template, overrides)
            workdir = template.backend.workdir

            async def _write(rf):
                await sandbox.write_file(
                    f"{workdir}/{rf.path}",
                    rf.content,
                    mode=int(rf.mode, 8) if rf.mode else None,
                )

            await self.materialize_files_on_backend(
                merged.files, _write, resolvers=resolvers,
            )
            # Run template init_commands in the pod, mirroring the
            # container backend: files land first, then each init command
            # execs via the same runtime client; a non-zero exit surfaces
            # as a ConfigError rather than being silently swallowed.
            init_cmds = merged.init_commands
            env_str = merged.env_unwrapped()
            for cmd in init_cmds:
                res = await sandbox.exec(
                    cmd, workdir=workdir, env=env_str,
                )
                if res.exit_code != 0:
                    raise ConfigError(
                        f"init command failed (rc={res.exit_code}): "
                        f"{cmd!r}\nstderr: {res.stderr}"
                    )
            runtime_meta = WorkspaceRuntimeMeta(
                url=url,
                token=SecretStr(token),
                k8s_object_name=obj_name,
            )
            ws = await SandboxWorkspace.materialise(
                workspace_id=workspace_id,
                template=template,
                sandbox=sandbox,
                backend_kind="kubernetes",
                runtime_meta=runtime_meta,
                workspace_root=template.backend.workdir,
            )
            async with self._lock:
                self._workspaces[workspace_id] = ws
                cached = True
            return ws
        except BaseException:
            # Anything that ends the build before the workspace is cached (a failure, a cancel, a caller's timeout) leaves
            # the connection open and nothing to close it: it is ours to release.
            if not cached:
                await close_shielded(client, what="rollback runtime client")
            raise

    async def _delete_best_effort(
        self, delete_call: Any, name: str, kind: str, *, during: str,
    ) -> None:
        """Delete one namespaced object; a 404 is fine and any other failure is logged, never raised."""
        try:
            await delete_call(name=name, namespace=self._config.namespace)
        except Exception as exc:  # noqa: BLE001
            if "404" not in str(exc):
                logger.warning("%s: %s delete for %r failed: %s", during, kind, name, exc)

    async def _roll_back_create(self, obj_name: str, created: list[str]) -> None:
        """Delete the cluster objects a ``create`` that did not finish made (``created``), newest first.

        Each on its own, so one that fails (logged) does not skip the next, and a 404 is fine; never raises. The PVC is made by
        the StatefulSet's volume claim template, so it goes with the StatefulSet.
        """
        assert self._core_v1 is not None and self._apps_v1 is not None
        made = set(created)
        if "httproute" in made:
            await self._destroy_httproute(obj_name)
        if "statefulset" in made:
            await self._delete_best_effort(
                self._apps_v1.delete_namespaced_stateful_set, obj_name, "statefulset", during="create rollback",
            )
            await self._delete_best_effort(
                self._core_v1.delete_namespaced_persistent_volume_claim, _pvc_name_for(obj_name), "pvc",
                during="create rollback",
            )
        if "service" in made:
            await self._delete_best_effort(
                self._core_v1.delete_namespaced_service, obj_name, "service", during="create rollback",
            )
        if "secret" in made:
            await self._delete_best_effort(
                self._core_v1.delete_namespaced_secret, obj_name, "secret", during="create rollback",
            )

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
            # The runtime container reads ``PRIMER_RUNTIME_TOKEN`` (see
            # primer_runtime.server.build_app); ``RUNTIME_TOKEN`` is the
            # operator-facing alias. The StatefulSet ``envFrom``s this Secret,
            # so every key becomes an env var -- carry both so the runtime
            # starts without the operator having to remap the name (mirrors
            # primer/workspace/runtime/docker.py).
            "stringData": {
                "PRIMER_RUNTIME_TOKEN": token,
                "RUNTIME_TOKEN": token,
            },
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

    async def _create_httproute(
        self, workspace_id: str, obj_name: str,
    ) -> None:
        """Create the per-workspace Gateway API HTTPRoute routing external
        traffic to the workspace's headless Service (gateway_httproute mode)."""
        assert isinstance(self._config.reachability, K8sReachabilityGateway)
        assert self._custom_objects is not None
        body = build_httproute_manifest(
            reachability=self._config.reachability,
            workspace_id=workspace_id,
            obj_name=obj_name,
            namespace=self._config.namespace,
        )
        await self._custom_objects.create_namespaced_custom_object(
            group="gateway.networking.k8s.io",
            version="v1",
            namespace=self._config.namespace,
            plural="httproutes",
            body=body,
        )

    async def _destroy_httproute(self, obj_name: str) -> None:
        """Best-effort delete of the per-workspace HTTPRoute; 404-tolerant."""
        if self._custom_objects is None:
            return
        try:
            await self._custom_objects.delete_namespaced_custom_object(
                group="gateway.networking.k8s.io",
                version="v1",
                namespace=self._config.namespace,
                plural="httproutes",
                name=obj_name,
            )
        except Exception as exc:  # noqa: BLE001
            if "404" not in str(exc):
                logger.warning(
                    "destroy: httproute delete for %r failed: %s", obj_name, exc,
                )

    async def _connect_runtime(self, url: str, token: str, *, workspace_id: str) -> RuntimeClient:
        """A connected :class:`RuntimeClient`, waiting for the runtime in a Running pod to start listening (ticket 01a11543).

        ``_wait_for_pod_running`` returns when the container has STARTED, before the runtime process serves, so the first connect
        can be refused (the first real ``materialise`` on k3s died with ``connect refused :5959``). The connect is retried while the
        failure means "not serving yet" (:func:`_runtime_not_serving_yet`), with a growing pause, until
        ``_RUNTIME_READY_TIMEOUT_S`` has passed; then a :class:`ConfigError` names the workspace and the last error, like the pod wait
        does. Anything else fails at once with its own error.

        Each attempt uses a FRESH client: a failed ``connect`` leaves its aiohttp session behind (and, after a hello timeout, maybe a
        half-open socket), and reusing the client would leak or reuse that. A failed attempt's client is closed before the next, and on a
        cancel; the one returned is the caller's to close (both callers do, on every path that does not cache it).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RUNTIME_READY_TIMEOUT_S
        pause = _RUNTIME_READY_POLL_S
        attempt = 0
        while True:
            attempt += 1
            client = RuntimeClient(url=url, token=token)
            try:
                await client.connect()
                if attempt > 1:
                    logger.info("workspace %s: the runtime accepted a connection on attempt %d", workspace_id, attempt)
                return client
            except BaseException as exc:
                await close_shielded(client, what=f"runtime client of workspace {workspace_id} (attempt {attempt})")
                if not isinstance(exc, Exception) or not _runtime_not_serving_yet(exc):
                    raise
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise ConfigError(
                        f"the runtime of workspace {workspace_id!r} did not accept a connection within "
                        f"{_RUNTIME_READY_TIMEOUT_S:g}s after its pod was Running ({attempt} attempts; last error: "
                        f"{type(exc).__name__}: {exc}); the runtime may be crash-looping or the gateway route may be missing, "
                        "check the pod's logs and the HTTPRoute"
                    ) from exc
                logger.info(
                    "workspace %s: the runtime is not serving yet (attempt %d: %s: %s); retrying in %.2fs",
                    workspace_id, attempt, type(exc).__name__, exc, min(pause, remaining),
                )
            await asyncio.sleep(min(pause, remaining))
            pause = min(pause * 2, _RUNTIME_READY_MAX_POLL_S)

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

    async def _reattach(
        self,
        workspace_id: str,
        template: WorkspaceTemplate | None,
    ) -> Workspace | None:
        """Re-attach to a workspace's StatefulSet after a cache miss.

        Called by :meth:`BaseWorkspaceBackend.get` only once the cache
        lookup (with gone-eviction) misses. The StatefulSet may exist from
        a previous process. We need a template to materialise the
        SandboxWorkspace wrapper; without one, return None and let the API
        layer re-issue with the template loaded from storage.
        """
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
        obj_name = k8s_object_name(workspace_id)
        try:
            await self._apps_v1.read_namespaced_stateful_set(
                obj_name, self._config.namespace,
            )
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc):
                return None
            raise
        pod_name = f"{obj_name}-0"
        # Make sure the Pod is up (the StatefulSet may have been scaled
        # to 0 between sessions).
        try:
            await self._wait_for_pod_running(pod_name)
        except ConfigError:
            # Scaled down -- bring it back up.
            await self._apps_v1.patch_namespaced_stateful_set_scale(
                obj_name, self._config.namespace,
                {"spec": {"replicas": 1}},
            )
            await self._wait_for_pod_running(pod_name)

        # Recover RUNTIME_TOKEN from the per-workspace Secret. Persisted
        # runtime_meta on the workspace row (Task 6.3) will eventually
        # subsume this; until then the Secret is the source of truth
        # because the platform doesn't keep tokens in process memory
        # across restarts.
        token = await self._read_runtime_token(obj_name)

        url = build_runtime_url(
            provider_config=self._config,
            workspace_id=workspace_id,
            k8s_object_name=obj_name,
        )
        client = await self._connect_runtime(url, token, workspace_id=workspace_id)
        released = False            # the client is no longer this function's to close: cached, or already being closed
        try:
            sandbox = WSSandbox(
                runtime_client=client,
                container_id=obj_name,
                workspace_root=template.backend.workdir,
            )
            reattach_meta = WorkspaceRuntimeMeta(
                url=url,
                token=SecretStr(token),
                k8s_object_name=obj_name,
            )
            ws = await SandboxWorkspace.materialise(
                workspace_id=workspace_id,
                template=template,
                sandbox=sandbox,
                backend_kind="kubernetes",
                runtime_meta=reattach_meta,
                workspace_root=template.backend.workdir,
            )
            async with self._lock:
                existing = self._workspaces.get(workspace_id)
                if existing is None:
                    self._workspaces[workspace_id] = ws
                    released = True                     # the cache owns the client now
                    return ws
            # Another caller materialised first: its workspace wins and OUR client is closed, outside the lock. Marked
            # released first, so a cancel that lands during the close does not close it a second time below.
            released = True
            await close_shielded(client, what="redundant runtime client")
            return existing
        except BaseException:
            # A failure, a cancel or a caller's timeout anywhere between connect() and the cache insert (including the wait
            # for the lock) leaves the connection open and uncached, and the next call connects and leaks another one.
            if not released:
                await close_shielded(client, what="re-attach runtime client")
            raise

    async def _read_runtime_token(self, obj_name: str) -> str:
        """Fetch ``RUNTIME_TOKEN`` out of the per-workspace Secret.

        Used by :meth:`get` to re-attach without holding the token in
        process memory. The Secret's ``stringData`` round-trips through
        the API server as base64 ``data``, so decode if needed.
        """
        assert self._core_v1 is not None
        secret = await self._core_v1.read_namespaced_secret(
            name=obj_name, namespace=self._config.namespace,
        )
        # kubernetes_asyncio returns a V1Secret with .data (b64-encoded)
        # and optionally .string_data on write. Some fakes round-trip via
        # .string_data only.
        data = getattr(secret, "data", None) or {}
        string_data = getattr(secret, "string_data", None) or {}
        # Prefer the canonical key the runtime reads; fall back to the alias
        # for Secrets minted before both keys were written.
        for key in ("PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"):
            if key in string_data:
                return string_data[key]
        import base64
        for key in ("PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"):
            raw = data.get(key)
            if raw is not None:
                return base64.b64decode(raw).decode("utf-8")
        raise ConfigError(
            f"Secret {obj_name!r} missing PRIMER_RUNTIME_TOKEN key"
        )

    async def list(self) -> list[str]:
        """Enumerate every workspace this backend manages.

        Reads the live StatefulSets in the configured namespace (label
        selector ``app.kubernetes.io/managed-by=primer``) and maps each one
        back to its workspace id via its ``primer.workspace.id`` label.
        This survives the API/worker process split and platform restarts:
        a workspace materialised by another process (or a previous run of
        this one) still appears, instead of only the in-memory handles this
        process happens to hold -- bringing K8s ``list()`` to parity with
        the container backend (which lists by container label) and the
        local backend's on-disk durability.

        Falls back to the in-memory set if the cluster query fails so a
        transient API error does not erase locally-known workspaces.
        """
        try:
            if not self._initialised:
                await self.initialize()
            assert self._apps_v1 is not None
            resp = await self._apps_v1.list_namespaced_stateful_set(
                namespace=self._config.namespace,
                label_selector="app.kubernetes.io/managed-by=primer",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "KubernetesWorkspaceBackend.list: cluster query failed; "
                "falling back to in-memory set: %s", exc,
            )
            return list(self._workspaces)
        ids: dict[str, None] = {}
        # Seed with in-memory handles so a workspace mid-create (STS not yet
        # visible) is not dropped from the listing.
        for wid in self._workspaces:
            ids[wid] = None
        for sts in getattr(resp, "items", []) or []:
            metadata = getattr(sts, "metadata", None)
            labels = getattr(metadata, "labels", None) or {}
            wid = (
                labels.get("primer.workspace.id")
                or labels.get("workspace-id")
            )
            if wid:
                ids[wid] = None
        return list(ids)

    async def destroy(self, workspace_id: str) -> None:
        """Tear down a workspace's Pod, Service, Secret, STS and PVC.

        Best-effort: missing objects are silently skipped so partial
        creates roll back cleanly. Raises :class:`NotFoundError` only
        when the StatefulSet itself doesn't exist (the canonical
        existence signal).
        """
        if not self._initialised:
            await self.initialize()
        assert self._core_v1 is not None and self._apps_v1 is not None

        async with self._lock:
            ws = self._workspaces.pop(workspace_id, None)
        if ws is not None:
            # Close the runtime WS first so reconnect attempts don't fire
            # while we tear the Pod down underneath them. The workspace's own
            # ``aclose`` only ends its sessions (over that connection); the
            # connection itself is its sandbox's client, which nothing else
            # closes, and one left open keeps reconnecting to a pod that no
            # longer exists (only a 404 handshake makes it give up). Both
            # closes are bounded: a silent peer must not hold up the teardown.
            await close_shielded(ws, what=f"workspace {workspace_id} during destroy")
            await close_shielded(ws.sandbox, what=f"workspace {workspace_id} runtime client during destroy")

        obj_name = k8s_object_name(workspace_id)
        ns = self._config.namespace
        try:
            await self._apps_v1.delete_namespaced_stateful_set(
                name=obj_name, namespace=ns,
            )
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc):
                if ws is None:
                    raise NotFoundError(
                        f"workspace {workspace_id!r} not found"
                    )
            else:
                raise
        # Best-effort cleanup of the Headless Service, Secret, and PVC.
        for delete_call, name, kind in (
            (self._core_v1.delete_namespaced_service, obj_name, "service"),
            (self._core_v1.delete_namespaced_secret, obj_name, "secret"),
            (
                self._core_v1.delete_namespaced_persistent_volume_claim,
                _pvc_name_for(obj_name), "pvc",
            ),
        ):
            await self._delete_best_effort(delete_call, name, kind, during="destroy")

        # Best-effort cleanup of the per-workspace HTTPRoute (gateway mode).
        if isinstance(self._config.reachability, K8sReachabilityGateway):
            await self._destroy_httproute(obj_name)


__all__ = ["KubernetesWorkspaceBackend"]
