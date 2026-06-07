"""Phase 5 K8s -> WS-runtime migration tests.

Verifies the building blocks added to ``primer.workspace.k8s.backend`` as
the K8s backend is rewritten to use the workspace-runtime protocol. Each
helper is exercised in isolation -- the full ``create()`` flow is wired
up in later Phase 5 tasks.
"""

from unittest.mock import AsyncMock

import pytest

from primer.model.workspace import (
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
    KubernetesWorkspaceConfig,
)
from primer.workspace.k8s.backend import KubernetesWorkspaceBackend


@pytest.mark.asyncio
async def test_create_namespaced_secret_called_with_runtime_token():
    """``_create_secret`` POSTs a Secret named ``primer-ws-<id>`` carrying
    a freshly-generated ``RUNTIME_TOKEN`` and labelled with the workspace
    id so the StatefulSet can later mount it via ``envFrom``."""

    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer-ns",
        reachability=K8sReachabilityInCluster(),
    )

    # The constructor still consumes the pre-Phase-1 config attrs; sidestep
    # it via __new__ + manual attribute assignment and inject a mock core_v1.
    backend = KubernetesWorkspaceBackend.__new__(KubernetesWorkspaceBackend)
    backend._config = cfg
    backend._core_v1 = AsyncMock()
    backend._core_v1.create_namespaced_secret = AsyncMock()

    workspace_id = "ws-abc-123"
    obj_name = "primer-ws-ws-abc-123"

    token = await backend._create_secret(workspace_id, obj_name)

    assert isinstance(token, str)
    assert len(token) >= 32

    backend._core_v1.create_namespaced_secret.assert_awaited_once()
    call = backend._core_v1.create_namespaced_secret.call_args
    body = call.kwargs.get("body")
    if body is None:
        # Fall back to positional (namespace, body).
        body = call.args[1]

    assert body["apiVersion"] == "v1"
    assert body["kind"] == "Secret"
    assert body["metadata"]["name"] == obj_name
    assert body["metadata"]["namespace"] == "primer-ns"
    assert body["metadata"]["labels"]["workspace-id"] == workspace_id
    assert (
        body["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "primer"
    )
    # The runtime container reads ``PRIMER_RUNTIME_TOKEN`` (see
    # primer_runtime.server.build_app); ``RUNTIME_TOKEN`` is the operator-
    # facing alias. Both must be present so the StatefulSet's ``envFrom``
    # injects the var the runtime actually starts up against -- without the
    # canonical key the pod crash-loops on a missing token.
    assert body["stringData"]["PRIMER_RUNTIME_TOKEN"] == token
    assert body["stringData"]["RUNTIME_TOKEN"] == token


@pytest.mark.asyncio
async def test_create_secret_targets_configured_namespace():
    """The namespace passed to ``create_namespaced_secret`` matches the
    provider config -- not hard-coded ``default``."""

    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="custom-ns",
        reachability=K8sReachabilityInCluster(),
    )

    backend = KubernetesWorkspaceBackend.__new__(KubernetesWorkspaceBackend)
    backend._config = cfg
    backend._core_v1 = AsyncMock()
    backend._core_v1.create_namespaced_secret = AsyncMock()

    await backend._create_secret("ws-xyz", "primer-ws-ws-xyz")

    call = backend._core_v1.create_namespaced_secret.call_args
    # Namespace may be passed as kwarg or first positional arg.
    ns = call.kwargs.get("namespace")
    if ns is None:
        ns = call.args[0]
    assert ns == "custom-ns"


@pytest.mark.asyncio
async def test_create_secret_generates_unique_tokens():
    """Two calls produce distinct tokens -- nothing is cached or reused."""

    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer-ns",
        reachability=K8sReachabilityInCluster(),
    )

    backend = KubernetesWorkspaceBackend.__new__(KubernetesWorkspaceBackend)
    backend._config = cfg
    backend._core_v1 = AsyncMock()
    backend._core_v1.create_namespaced_secret = AsyncMock()

    t1 = await backend._create_secret("ws-a", "primer-ws-ws-a")
    t2 = await backend._create_secret("ws-b", "primer-ws-ws-b")
    assert t1 != t2


@pytest.mark.asyncio
async def test_create_namespaced_service_creates_headless_service():
    """Headless Service manifest: clusterIP=None, selector matches workspace-id."""
    from primer.workspace.k8s.backend import KubernetesWorkspaceBackend
    from primer.model.workspace import (
        KubernetesWorkspaceConfig, K8sConnectionInCluster, K8sReachabilityInCluster,
    )

    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer-ns",
        reachability=K8sReachabilityInCluster(),
    )
    backend = KubernetesWorkspaceBackend.__new__(KubernetesWorkspaceBackend)
    backend._config = cfg
    backend._core_v1 = AsyncMock()
    backend._core_v1.create_namespaced_service = AsyncMock()

    await backend._create_service("ws-1", "primer-ws-ws-1")
    backend._core_v1.create_namespaced_service.assert_awaited_once()
    call_kwargs = backend._core_v1.create_namespaced_service.call_args.kwargs
    body = call_kwargs.get("body", backend._core_v1.create_namespaced_service.call_args.args[1] if len(backend._core_v1.create_namespaced_service.call_args.args) > 1 else None)
    assert body["metadata"]["name"] == "primer-ws-ws-1"
    assert body["metadata"]["namespace"] == "primer-ns"
    assert body["metadata"]["labels"]["workspace-id"] == "ws-1"
    assert body["spec"]["clusterIP"] == "None"
    assert body["spec"]["selector"] == {"workspace-id": "ws-1"}
    assert body["spec"]["ports"][0]["port"] == 5959
    assert body["spec"]["ports"][0]["targetPort"] == 5959
    assert body["spec"]["ports"][0]["name"] == "runtime"


# ---------------------------------------------------------------------------
# Task 5.4: StatefulSet manifest binds to Secret + Headless Service.
# ---------------------------------------------------------------------------


def _statefulset_test_inputs(workspace_id: str = "ws-1"):
    """Smallest viable (template, provider_cfg, obj_name) for the manifest
    builder under the *current* model shape (post-c8dd6ce5)."""
    from primer.model.workspace import (
        K8sConnectionInCluster,
        K8sReachabilityInCluster,
        KubernetesTemplateConfig,
        KubernetesWorkspaceConfig,
        WorkspaceTemplate,
    )
    from primer.workspace.k8s.naming import k8s_object_name

    template = WorkspaceTemplate(
        id="t1",
        provider_id="k1",
        description="",
        backend=KubernetesTemplateConfig(image="python:3.13"),
    )
    provider_cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer-ns",
        reachability=K8sReachabilityInCluster(),
    )
    return template, provider_cfg, k8s_object_name(workspace_id)


@pytest.mark.asyncio
async def test_statefulset_manifest_has_envfrom_secret():
    """The pod template envFrom the per-workspace Secret so the container
    inherits RUNTIME_TOKEN at start-up."""
    from primer.workspace.k8s.backend import _build_statefulset_manifest

    template, provider_cfg, obj_name = _statefulset_test_inputs("ws-1")
    m = _build_statefulset_manifest(
        sts_name=obj_name,
        namespace="primer-ns",
        workspace_id="ws-1",
        template=template,
        provider_cfg=provider_cfg,
        obj_name=obj_name,
    )
    container = m["spec"]["template"]["spec"]["containers"][0]
    env_from = container.get("envFrom", [])
    secret_refs = [
        e["secretRef"]["name"]
        for e in env_from
        if isinstance(e, dict) and "secretRef" in e
    ]
    assert obj_name in secret_refs, (
        f"expected secretRef named {obj_name!r} in container.envFrom; "
        f"got {env_from!r}"
    )


@pytest.mark.asyncio
async def test_statefulset_manifest_has_runtime_port():
    """Container ports include a 'runtime' port at 5959, matching the
    Headless Service's targetPort."""
    from primer.workspace.k8s.backend import _build_statefulset_manifest

    template, provider_cfg, obj_name = _statefulset_test_inputs("ws-1")
    m = _build_statefulset_manifest(
        sts_name=obj_name,
        namespace="primer-ns",
        workspace_id="ws-1",
        template=template,
        provider_cfg=provider_cfg,
        obj_name=obj_name,
    )
    container = m["spec"]["template"]["spec"]["containers"][0]
    ports = container.get("ports", [])
    runtime_ports = [
        p for p in ports
        if isinstance(p, dict) and p.get("name") == "runtime"
    ]
    assert len(runtime_ports) == 1, f"expected one runtime port; got {ports!r}"
    assert runtime_ports[0]["containerPort"] == 5959


@pytest.mark.asyncio
async def test_statefulset_manifest_pod_template_has_workspace_id_label():
    """Pod template labels include workspace-id=<workspace_id>, matching
    the Headless Service's selector."""
    from primer.workspace.k8s.backend import _build_statefulset_manifest

    template, provider_cfg, obj_name = _statefulset_test_inputs("ws-1")
    m = _build_statefulset_manifest(
        sts_name=obj_name,
        namespace="primer-ns",
        workspace_id="ws-1",
        template=template,
        provider_cfg=provider_cfg,
        obj_name=obj_name,
    )
    pod_labels = m["spec"]["template"]["metadata"]["labels"]
    assert pod_labels.get("workspace-id") == "ws-1"


@pytest.mark.asyncio
async def test_statefulset_manifest_service_name_matches_obj_name():
    """spec.serviceName binds the STS pods to the per-workspace Headless
    Service (whose name is obj_name)."""
    from primer.workspace.k8s.backend import _build_statefulset_manifest

    template, provider_cfg, obj_name = _statefulset_test_inputs("ws-1")
    m = _build_statefulset_manifest(
        sts_name=obj_name,
        namespace="primer-ns",
        workspace_id="ws-1",
        template=template,
        provider_cfg=provider_cfg,
        obj_name=obj_name,
    )
    assert m["spec"]["serviceName"] == obj_name


# ---------------------------------------------------------------------------
# Task 5.5: create()/get() return a SandboxWorkspace over a WSSandbox.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_returns_sandbox_workspace_with_wssandbox():
    """K8s backend's ``create()`` returns a :class:`SandboxWorkspace` whose
    inner :class:`Sandbox` is a :class:`WSSandbox` over a connected
    :class:`RuntimeClient` -- not the legacy tar-over-exec ``K8sSandbox``."""
    from unittest.mock import patch

    from primer.model.workspace import (
        KubernetesTemplateConfig,
        WorkspaceTemplate,
    )
    from primer.workspace.runtime.ws_sandbox import WSSandbox
    from primer.workspace.sandbox.workspace import SandboxWorkspace

    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer-ns",
        reachability=K8sReachabilityInCluster(),
    )
    backend = KubernetesWorkspaceBackend.__new__(KubernetesWorkspaceBackend)
    backend._config = cfg
    backend._core_v1 = AsyncMock()
    backend._apps_v1 = AsyncMock()
    backend._workspaces = {}
    backend._lock = __import__("asyncio").Lock()
    backend._initialised = True
    backend._core_v1.create_namespaced_secret = AsyncMock()
    backend._core_v1.create_namespaced_service = AsyncMock()
    backend._apps_v1.create_namespaced_stateful_set = AsyncMock()
    backend._wait_for_pod_running = AsyncMock()

    template = WorkspaceTemplate(
        id="tpl-1",
        provider_id="prov-1",
        description="",
        backend=KubernetesTemplateConfig(image="primer-runtime:1"),
    )

    # Stub RuntimeClient so we don't open a real WS connection.
    with patch("primer.workspace.k8s.backend.RuntimeClient") as MockRC:
        mock_client = AsyncMock()
        MockRC.return_value = mock_client
        ws = await backend.create(
            template,
            overrides=None,
            workspace_id="ws-1",
        )

    assert isinstance(ws, SandboxWorkspace)
    inner_sandbox = getattr(ws, "_sandbox", None)
    assert inner_sandbox is not None
    assert isinstance(inner_sandbox, WSSandbox)
    # RuntimeClient was constructed with the matching URL + token from the
    # Secret we just created.
    assert MockRC.call_count == 1
    rc_kwargs = MockRC.call_args.kwargs
    assert "url" in rc_kwargs and "token" in rc_kwargs
    assert rc_kwargs["url"].startswith("ws://primer-ws-ws-1-0.primer-ws-ws-1.")
    # The token must be the one stored in the per-workspace Secret.
    secret_body = backend._core_v1.create_namespaced_secret.call_args.kwargs[
        "body"
    ]
    assert rc_kwargs["token"] == secret_body["stringData"]["RUNTIME_TOKEN"]
    # And the WS connection was actually opened.
    mock_client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_returns_sandbox_workspace_after_reattach():
    """``get()`` re-attaches to an existing STS by reading RUNTIME_TOKEN out
    of the Secret and returning a :class:`SandboxWorkspace` over a fresh
    :class:`WSSandbox`."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from primer.model.workspace import (
        KubernetesTemplateConfig,
        WorkspaceTemplate,
    )
    from primer.workspace.runtime.ws_sandbox import WSSandbox
    from primer.workspace.sandbox.workspace import SandboxWorkspace

    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer-ns",
        reachability=K8sReachabilityInCluster(),
    )
    backend = KubernetesWorkspaceBackend.__new__(KubernetesWorkspaceBackend)
    backend._config = cfg
    backend._core_v1 = AsyncMock()
    backend._apps_v1 = AsyncMock()
    backend._workspaces = {}
    backend._lock = __import__("asyncio").Lock()
    backend._initialised = True
    # STS exists.
    backend._apps_v1.read_namespaced_stateful_set = AsyncMock()
    # Secret carries the token.
    backend._core_v1.read_namespaced_secret = AsyncMock(
        return_value=SimpleNamespace(
            data=None,
            string_data={"RUNTIME_TOKEN": "the-stored-token"},
        )
    )
    backend._wait_for_pod_running = AsyncMock()

    template = WorkspaceTemplate(
        id="tpl-1",
        provider_id="prov-1",
        description="",
        backend=KubernetesTemplateConfig(image="primer-runtime:1"),
    )

    with patch("primer.workspace.k8s.backend.RuntimeClient") as MockRC:
        mock_client = AsyncMock()
        MockRC.return_value = mock_client
        ws = await backend.get("ws-1", template=template)

    assert isinstance(ws, SandboxWorkspace)
    assert isinstance(ws._sandbox, WSSandbox)
    # The token plumbed into RuntimeClient came from the Secret read.
    rc_kwargs = MockRC.call_args.kwargs
    assert rc_kwargs["token"] == "the-stored-token"
    mock_client.connect.assert_awaited_once()


def test_gateway_reachability_hostname_round_trips():
    from primer.model.workspace import (
        K8sReachabilityGateway,
        K8sGatewayParentRef,
        K8sGatewayRoutingHostname,
        KubernetesWorkspaceConfig,
        K8sConnectionInCluster,
    )
    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer-workspaces",
        reachability=K8sReachabilityGateway(
            gateway=K8sGatewayParentRef(name="primer-gw", namespace="primer-gateway"),
            routing=K8sGatewayRoutingHostname(hostname_template="{workspace_id}.ws.local"),
            external_port=32045,
        ),
    )
    reparsed = KubernetesWorkspaceConfig.model_validate(cfg.model_dump())
    assert reparsed.reachability.kind == "gateway_httproute"
    assert reparsed.reachability.routing.kind == "hostname"
    assert reparsed.reachability.scheme == "ws"
    assert reparsed.reachability.backend_port == 5959


def test_gateway_parent_ref_rejects_unknown_field():
    from pydantic import ValidationError
    from primer.model.workspace import K8sGatewayParentRef
    with pytest.raises(ValidationError):
        K8sGatewayParentRef(name="gw", typo_field="x")


def test_gateway_reachability_path_mode_parses():
    from primer.model.workspace import (
        K8sReachabilityGateway,
        K8sGatewayParentRef,
        K8sGatewayRoutingPath,
        KubernetesWorkspaceConfig,
        K8sConnectionInCluster,
    )
    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer-workspaces",
        reachability=K8sReachabilityGateway(
            gateway=K8sGatewayParentRef(name="primer-gw"),
            routing=K8sGatewayRoutingPath(hostname="ws.local"),
            external_port=8000,
            scheme="wss",
        ),
    )
    reparsed = KubernetesWorkspaceConfig.model_validate(cfg.model_dump())
    assert reparsed.reachability.routing.kind == "path_prefix"
    assert reparsed.reachability.routing.path_template == "/ws/{workspace_id}"
    assert reparsed.reachability.scheme == "wss"
